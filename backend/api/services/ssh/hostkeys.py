"""Host-key verification backed by the ``ssh_host_keys`` table.

Policies:
  strict — the key must already be recorded (any source); unknown ⇒ refuse.
  pinned — the key must be recorded with source=preapproved; TOFU rows don't count.
  tofu   — unknown keys are recorded on first use and trusted thereafter; a
           *changed* fingerprint is always refused (that's the attack TOFU exists
           to catch).
"""

from __future__ import annotations

import hashlib
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
