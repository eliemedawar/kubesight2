"""SSH credentials + connection profiles: CRUD, serialization, target building.

Secrets (private keys, passwords, sudo passwords) are Fernet-encrypted at rest
and never serialized back to the UI — responses carry ``secretConfigured``
booleans instead, mirroring ``registry_service``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..db import db
from ..models import SshConnectionProfile, SshCredential
from ..secret_encryption import decrypt_secret, encrypt_secret
from .ssh import SshConnectionError, SshTarget, get_transport

_AUTH_METHODS = {"key", "password"}
_SUDO_MODES = {"root", "nopasswd", "password"}
_ROUTE_MODES = {"direct", "bastion"}
_HOST_KEY_POLICIES = {"strict", "tofu", "pinned"}


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def serialize_credential(row: SshCredential) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "username": row.username,
        "authMethod": row.auth_method,
        "port": row.port,
        "sudoMode": row.sudo_mode,
        "secretConfigured": bool(row.secret_cipher),
        "sudoPasswordConfigured": bool(row.sudo_password_cipher),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_credentials() -> List[Dict[str, Any]]:
    rows = SshCredential.query.order_by(SshCredential.name.asc()).all()
    return [serialize_credential(row) for row in rows]


def get_credential(credential_id: int) -> SshCredential:
    row = db.session.get(SshCredential, credential_id)
    if row is None:
        raise LookupError("SSH credential not found.")
    return row


def _apply_credential_payload(row: SshCredential, payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", row.name or "")).strip()
    username = str(payload.get("username", row.username or "")).strip()
    if not name:
        raise ValueError("name is required.")
    if not username:
        raise ValueError("username is required.")
    auth_method = str(payload.get("authMethod", row.auth_method or "key")).strip()
    if auth_method not in _AUTH_METHODS:
        raise ValueError("authMethod must be 'key' or 'password'.")
    sudo_mode = str(payload.get("sudoMode", row.sudo_mode or "nopasswd")).strip()
    if sudo_mode not in _SUDO_MODES:
        raise ValueError("sudoMode must be 'root', 'nopasswd', or 'password'.")
    try:
        port = int(payload.get("port", row.port or 22))
    except (TypeError, ValueError) as exc:
        raise ValueError("port must be an integer.") from exc
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535.")

    row.name = name
    row.username = username
    row.auth_method = auth_method
    row.sudo_mode = sudo_mode
    row.port = port

    # Secret fields: only overwritten when the payload carries a value, so an
    # edit that doesn't retype the key keeps the stored one.
    secret = payload.get("secret")
    if secret:
        row.secret_cipher = encrypt_secret(str(secret))
    passphrase = payload.get("keyPassphrase")
    if passphrase:
        row.key_passphrase_cipher = encrypt_secret(str(passphrase))
    sudo_password = payload.get("sudoPassword")
    if sudo_password:
        row.sudo_password_cipher = encrypt_secret(str(sudo_password))
    if sudo_mode != "password":
        row.sudo_password_cipher = None
    if not row.secret_cipher:
        raise ValueError(
            "secret is required (private key PEM for key auth, password for "
            "password auth)."
        )


def create_credential(payload: Dict[str, Any], created_by: str = "") -> Dict[str, Any]:
    row = SshCredential(created_by=created_by or None)
    _apply_credential_payload(row, payload)
    db.session.add(row)
    db.session.commit()
    return serialize_credential(row)


def update_credential(credential_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_credential(credential_id)
    _apply_credential_payload(row, payload)
    db.session.commit()
    return serialize_credential(row)


def delete_credential(credential_id: int) -> None:
    row = get_credential(credential_id)
    in_use = SshConnectionProfile.query.filter(
        (SshConnectionProfile.credential_id == credential_id)
        | (SshConnectionProfile.bastion_credential_id == credential_id)
    ).count()
    if in_use:
        raise ValueError(
            f"Credential is referenced by {in_use} connection profile(s); "
            "remove those first."
        )
    db.session.delete(row)
    db.session.commit()


# ---------------------------------------------------------------------------
# Connection profiles
# ---------------------------------------------------------------------------

def serialize_profile(row: SshConnectionProfile) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "credentialId": row.credential_id,
        "credentialName": row.credential.name if row.credential else None,
        "routeMode": row.route_mode,
        "bastionHost": row.bastion_host,
        "bastionPort": row.bastion_port,
        "bastionCredentialId": row.bastion_credential_id,
        "hostKeyPolicy": row.host_key_policy,
        "connectTimeoutS": row.connect_timeout_s,
        "commandTimeoutS": row.command_timeout_s,
        "retryCount": row.retry_count,
        "retryBackoffS": row.retry_backoff_s,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_profiles() -> List[Dict[str, Any]]:
    rows = SshConnectionProfile.query.order_by(SshConnectionProfile.name.asc()).all()
    return [serialize_profile(row) for row in rows]


def get_profile(profile_id: int) -> SshConnectionProfile:
    row = db.session.get(SshConnectionProfile, profile_id)
    if row is None:
        raise LookupError("SSH connection profile not found.")
    return row


def _apply_profile_payload(row: SshConnectionProfile, payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", row.name or "")).strip()
    if not name:
        raise ValueError("name is required.")
    credential_id = payload.get("credentialId", row.credential_id)
    if not credential_id:
        raise ValueError("credentialId is required.")
    get_credential(int(credential_id))  # raises LookupError if missing

    route_mode = str(payload.get("routeMode", row.route_mode or "direct")).strip()
    if route_mode not in _ROUTE_MODES:
        raise ValueError("routeMode must be 'direct' or 'bastion'.")
    policy = str(payload.get("hostKeyPolicy", row.host_key_policy or "tofu")).strip()
    if policy not in _HOST_KEY_POLICIES:
        raise ValueError("hostKeyPolicy must be 'strict', 'tofu', or 'pinned'.")

    bastion_host = str(payload.get("bastionHost", row.bastion_host or "") or "").strip()
    bastion_credential_id = payload.get("bastionCredentialId", row.bastion_credential_id)
    if route_mode == "bastion":
        if not bastion_host:
            raise ValueError("bastionHost is required for bastion routing.")
        if not bastion_credential_id:
            raise ValueError("bastionCredentialId is required for bastion routing.")
        get_credential(int(bastion_credential_id))
    else:
        bastion_host = ""
        bastion_credential_id = None

    def _int_field(key: str, current: int, low: int, high: int) -> int:
        try:
            value = int(payload.get(key, current))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer.") from exc
        if value < low or value > high:
            raise ValueError(f"{key} must be between {low} and {high}.")
        return value

    row.name = name
    row.credential_id = int(credential_id)
    row.route_mode = route_mode
    row.bastion_host = bastion_host or None
    row.bastion_port = (
        _int_field("bastionPort", row.bastion_port or 22, 1, 65535)
        if route_mode == "bastion"
        else None
    )
    row.bastion_credential_id = (
        int(bastion_credential_id) if bastion_credential_id else None
    )
    row.host_key_policy = policy
    row.connect_timeout_s = _int_field("connectTimeoutS", row.connect_timeout_s or 15, 1, 300)
    row.command_timeout_s = _int_field("commandTimeoutS", row.command_timeout_s or 600, 10, 7200)
    row.retry_count = _int_field("retryCount", row.retry_count or 2, 0, 10)
    row.retry_backoff_s = _int_field("retryBackoffS", row.retry_backoff_s or 5, 1, 120)


def create_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = SshConnectionProfile(credential_id=0)
    _apply_profile_payload(row, payload)
    db.session.add(row)
    db.session.commit()
    return serialize_profile(row)


def update_profile(profile_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_profile(profile_id)
    _apply_profile_payload(row, payload)
    db.session.commit()
    return serialize_profile(row)


def delete_profile(profile_id: int) -> None:
    from ..models import ClusterBuild, ClusterBuildNode

    row = get_profile(profile_id)
    in_use = (
        ClusterBuild.query.filter_by(connection_profile_id=profile_id).count()
        + ClusterBuildNode.query.filter_by(connection_profile_id=profile_id).count()
    )
    if in_use:
        raise ValueError("Profile is referenced by cluster builds; remove those first.")
    db.session.delete(row)
    db.session.commit()


# ---------------------------------------------------------------------------
# Target building + connectivity test
# ---------------------------------------------------------------------------

def _credential_target_fields(cred: SshCredential) -> Dict[str, Any]:
    secret = decrypt_secret(cred.secret_cipher or "")
    return {
        "username": cred.username,
        "auth_method": cred.auth_method,
        "private_key_pem": secret if cred.auth_method == "key" else "",
        "key_passphrase": decrypt_secret(cred.key_passphrase_cipher or ""),
        "password": secret if cred.auth_method == "password" else "",
        "sudo_mode": "root" if cred.username == "root" else cred.sudo_mode,
        "sudo_password": decrypt_secret(cred.sudo_password_cipher or ""),
        "port": cred.port,
    }


def build_target(
    profile: SshConnectionProfile, host: str, *, label: str = ""
) -> SshTarget:
    """Materialize an SshTarget (decrypted, in-memory only) for one host."""
    cred_fields = _credential_target_fields(profile.credential)
    bastion = None
    if profile.route_mode == "bastion" and profile.bastion_host:
        bastion_cred = profile.bastion_credential or profile.credential
        bastion_fields = _credential_target_fields(bastion_cred)
        bastion = SshTarget(
            host=profile.bastion_host,
            port=profile.bastion_port or bastion_fields.pop("port"),
            host_key_policy=profile.host_key_policy,
            connect_timeout_s=profile.connect_timeout_s,
            label=f"bastion {profile.bastion_host}",
            **{k: v for k, v in bastion_fields.items() if k != "port"},
        )
    port = cred_fields.pop("port")
    return SshTarget(
        host=host,
        port=port,
        host_key_policy=profile.host_key_policy,
        connect_timeout_s=profile.connect_timeout_s,
        command_timeout_s=profile.command_timeout_s,
        retry_count=profile.retry_count,
        retry_backoff_s=profile.retry_backoff_s,
        bastion=bastion,
        label=label or host,
        **cred_fields,
    )


def test_profile(profile_id: int, host: str) -> Dict[str, Any]:
    """Connect to ``host`` through the profile and prove login + escalation."""
    profile = get_profile(profile_id)
    host = str(host or "").strip()
    if not host:
        raise ValueError("host is required.")
    target = build_target(profile, host, label=host)
    transport = get_transport()
    started = datetime.now(timezone.utc)
    try:
        result = transport.run(
            target, "id -un && uname -sr", timeout_s=30
        )
        elapsed_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        lines = [line for line in result.output.strip().splitlines() if line.strip()]
        return {
            "status": "ok",
            "effectiveUser": lines[0] if lines else "",
            "kernel": lines[1] if len(lines) > 1 else "",
            "latencyMs": elapsed_ms,
        }
    except (SshConnectionError, Exception) as exc:  # noqa: BLE001
        message = getattr(exc, "output", "") or str(exc)
        return {"status": "failed", "error": str(exc), "output": message[:2000]}
