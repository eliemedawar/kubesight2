"""Host-key verification backed by the ``ssh_host_keys`` table.

Policies:
  strict — the key must already be recorded (any source); unknown ⇒ refuse.
  pinned — the key must be recorded with source=preapproved; TOFU rows don't count.
  tofu   — unknown keys are recorded on first use and trusted thereafter; a
           *changed* fingerprint is always refused (that's the attack TOFU exists
           to catch).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

from ...db import db
from ...models import SshHostKey


def fingerprint_sha256(key_bytes: bytes) -> str:
    return hashlib.sha256(key_bytes).hexdigest()


def _find(host: str, port: int, key_type: str) -> Optional[SshHostKey]:
    return SshHostKey.query.filter_by(host=host, port=port, key_type=key_type).first()


def verify_host_key(
    *, host: str, port: int, key_type: str, fingerprint_sha256: str, policy: str
) -> Tuple[bool, str]:
    """Returns (ok, reason). Never raises — the transport turns a False into a
    refused connection with the reason in the message."""
    row = _find(host, port, key_type)

    if row is not None:
        if row.fingerprint_sha256 == fingerprint_sha256:
            if policy == "pinned" and row.source != "preapproved":
                return False, (
                    "policy is 'pinned' but the recorded key came from TOFU; "
                    "pre-approve the fingerprint to allow this host."
                )
            return True, "recorded"
        return False, (
            f"host key CHANGED for {host}:{port} ({key_type}). Recorded "
            f"{row.fingerprint_sha256[:16]}…, presented "
            f"{fingerprint_sha256[:16]}…. If the host was legitimately "
            "rebuilt, delete the recorded key and re-approve."
        )

    if policy == "tofu":
        db.session.add(
            SshHostKey(
                host=host,
                port=port,
                key_type=key_type,
                fingerprint_sha256=fingerprint_sha256,
                source="tofu",
            )
        )
        db.session.commit()
        return True, "recorded on first use (tofu)"

    return False, (
        f"no recorded host key for {host}:{port} and policy is '{policy}'. "
        "Pre-approve the host's SSH fingerprint or use a TOFU profile."
    )


def preapprove(
    *, host: str, port: int, key_type: str, fingerprint_sha256: str, user_id=None
) -> SshHostKey:
    """Record (or upgrade) a fingerprint as pre-approved."""
    row = _find(host, port, key_type)
    if row is None:
        row = SshHostKey(host=host, port=port, key_type=key_type)
        db.session.add(row)
    row.fingerprint_sha256 = fingerprint_sha256
    row.source = "preapproved"
    row.approved_by_user_id = user_id
    row.approved_at = datetime.now(timezone.utc)
    db.session.commit()
    return row


# ---------------------------------------------------------------------------
# Management (Sources tab): list / scan / pin / delete
# ---------------------------------------------------------------------------

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class HostKeyError(ValueError):
    """Bad input for a host-key management call."""


def openssh_fingerprint(hex_digest: str) -> str:
    """Hex SHA-256 (how rows store it) → the ``SHA256:…`` form ``ssh-keygen -lf``
    and ``ssh-keyscan | ssh-keygen -lf -`` print, for out-of-band comparison."""
    try:
        raw = bytes.fromhex(hex_digest or "")
    except ValueError:
        return ""
    if not raw:
        return ""
    return "SHA256:" + base64.b64encode(raw).decode("ascii").rstrip("=")


def normalize_fingerprint(value: str) -> str:
    """Accept hex SHA-256 (optionally colon-separated) or OpenSSH ``SHA256:b64``."""
    text = str(value or "").strip()
    if text.upper().startswith("SHA256:"):
        b64 = text.split(":", 1)[1].strip()
        try:
            raw = base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HostKeyError("fingerprint is not valid SHA256 base64.") from exc
        if len(raw) != 32:
            raise HostKeyError("a SHA256 fingerprint decodes to 32 bytes.")
        return raw.hex()
    hex_digest = text.replace(":", "").lower()
    if not _HEX64.match(hex_digest):
        raise HostKeyError(
            "fingerprint must be a SHA256 digest — 'SHA256:…' as printed by "
            "ssh-keygen -lf, or 64 hex characters."
        )
    return hex_digest


def validate_host_port(host: str, port) -> Tuple[str, int]:
    host = str(host or "").strip()
    if not host or len(host) > 253 or not re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-:]*$", host):
        raise HostKeyError("host must be a hostname or IP address.")
    try:
        port = int(port if port not in (None, "") else 22)
    except (TypeError, ValueError) as exc:
        raise HostKeyError("port must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise HostKeyError("port must be between 1 and 65535.")
    return host, port


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def serialize_host_key(row: SshHostKey) -> dict:
    approver = None
    if row.approved_by_user_id:
        from ...models import User

        user = db.session.get(User, row.approved_by_user_id)
        approver = user.username if user else None
    return {
        "id": row.id,
        "host": row.host,
        "port": row.port,
        "keyType": row.key_type,
        "fingerprintSha256": row.fingerprint_sha256,
        "fingerprint": openssh_fingerprint(row.fingerprint_sha256),
        "source": row.source,
        "approvedBy": approver,
        "approvedAt": _iso(row.approved_at),
        "createdAt": _iso(row.created_at),
    }


def list_host_keys() -> list:
    rows = SshHostKey.query.order_by(
        SshHostKey.host.asc(), SshHostKey.port.asc(), SshHostKey.key_type.asc()
    ).all()
    return [serialize_host_key(row) for row in rows]


def get_host_key(key_id: int) -> SshHostKey:
    row = db.session.get(SshHostKey, int(key_id))
    if row is None:
        raise LookupError("Host key not found.")
    return row


def delete_host_key(key_id: int) -> dict:
    row = get_host_key(key_id)
    snapshot = serialize_host_key(row)
    db.session.delete(row)
    db.session.commit()
    return snapshot


def scan_host(host: str, port=22, *, bastion=None, timeout_s: int = 10) -> dict:
    """Current key the host presents, compared with what is recorded.

    Nothing is trusted or written. ``status``: ``unknown`` (nothing recorded
    for this key type), ``match`` (recorded and identical — ``recorded.source``
    says whether it is already pinned), or ``changed`` (recorded and DIFFERENT).
    """
    from .transport import get_transport

    host, port = validate_host_port(host, port)
    transport = get_transport()
    scanner = getattr(transport, "scan_host_key", None)
    if scanner is None:
        raise HostKeyError("The configured SSH transport cannot scan host keys.")
    presented = scanner(host, port, timeout_s=timeout_s, bastion=bastion)
    key_type = str(presented.get("keyType") or "")
    digest = fingerprint_sha256(presented["keyBytes"])
    row = _find(host, port, key_type)
    if row is None:
        status = "unknown"
    elif row.fingerprint_sha256 == digest:
        status = "match"
    else:
        status = "changed"
    return {
        "host": host,
        "port": port,
        "keyType": key_type,
        "fingerprintSha256": digest,
        "fingerprint": openssh_fingerprint(digest),
        "status": status,
        "recorded": serialize_host_key(row) if row is not None else None,
    }


def pin_host_key(
    *, host: str, port, key_type: str, fingerprint: str, user_id=None
) -> Tuple[dict, Optional[dict]]:
    """Validate then ``preapprove``. Returns (new row, previous row or None) so
    the route can audit a replaced fingerprint distinctly from a first pin."""
    host, port = validate_host_port(host, port)
    key_type = str(key_type or "").strip()
    if not key_type or len(key_type) > 32 or not re.match(r"^[A-Za-z0-9@._\-]+$", key_type):
        raise HostKeyError("keyType is required (e.g. ssh-ed25519, ecdsa-sha2-nistp256).")
    digest = normalize_fingerprint(fingerprint)
    existing = _find(host, port, key_type)
    previous = serialize_host_key(existing) if existing is not None else None
    row = preapprove(
        host=host, port=port, key_type=key_type, fingerprint_sha256=digest, user_id=user_id
    )
    return serialize_host_key(row), previous
