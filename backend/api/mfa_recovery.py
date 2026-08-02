"""Hash-only MFA recovery codes and local break-glass administration."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .access_engine import is_admin
from .audit import log_audit
from .db import db
from .models import User
from .models_auth import AdminRecoveryGrant, MfaRecoveryCode
from .session_auth import revoke_all_user_sessions


_RECOVERY_CODE_BYTES = 10  # 80 bits; rendered as 16 Base32 characters.
_DEFAULT_RECOVERY_CODE_COUNT = 10
_MAX_RECOVERY_CODE_COUNT = 20
_BASE32_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_recovery_code(value: str) -> str:
    normalized: list[str] = []
    for character in (value or "").strip().upper():
        if character in _BASE32_ALPHABET:
            normalized.append(character)
        elif character not in {"-", " ", "\t"}:
            return ""
    return "".join(normalized)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_recovery_code() -> str:
    normalized = base64.b32encode(secrets.token_bytes(_RECOVERY_CODE_BYTES)).decode(
        "ascii"
    ).rstrip("=")
    return "-".join(
        normalized[index : index + 4] for index in range(0, len(normalized), 4)
    )


def regenerate_recovery_codes(
    user: User, *, count: int = _DEFAULT_RECOVERY_CODE_COUNT
) -> list[str]:
    """Invalidate old codes and return a new plaintext set exactly once."""
    count = int(count)
    if count < 1 or count > _MAX_RECOVERY_CODE_COUNT:
        raise ValueError(
            f"Recovery code count must be between 1 and {_MAX_RECOVERY_CODE_COUNT}."
        )
    codes: list[str] = []
    normalized_codes: set[str] = set()
    while len(codes) < count:
        code = _new_recovery_code()
        normalized = _normalize_recovery_code(code)
        if normalized in normalized_codes:
            continue
        normalized_codes.add(normalized)
        codes.append(code)

    MfaRecoveryCode.query.filter_by(user_id=user.id).delete(
        synchronize_session=False
    )
    db.session.add_all(
        MfaRecoveryCode(user_id=user.id, code_hash=_hash(normalized))
        for normalized in normalized_codes
    )
    log_audit(
        "mfa_recovery_codes_regenerated",
        actor=user,
        target_type="user",
        target_id=user.id,
        details={"count": len(codes)},
        commit=False,
    )
    db.session.commit()
    return codes


def consume_recovery_code(user: User, code: str) -> bool:
    """Atomically consume one code; concurrent reuse has exactly one winner."""
    normalized = _normalize_recovery_code(code)
    now = _utcnow()
    consumed = 0
    if len(normalized) == 16:
        consumed = MfaRecoveryCode.query.filter(
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.code_hash == _hash(normalized),
            MfaRecoveryCode.used_at.is_(None),
        ).update({MfaRecoveryCode.used_at: now}, synchronize_session=False)
    action = (
        "mfa_recovery_code_used" if consumed == 1 else "mfa_recovery_code_rejected"
    )
    log_audit(
        action,
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        details={"outcome": "success" if consumed == 1 else "rejected"},
        commit=False,
    )
    db.session.commit()
    return consumed == 1


def recovery_code_count(user: User) -> int:
    return MfaRecoveryCode.query.filter_by(user_id=user.id, used_at=None).count()


def complete_login_with_recovery_code(user: User, code: str):
    """Use one recovery code and rejoin the canonical login completion path."""
    if not user.mfa_enabled or not user.totp_secret:
        return None, "MFA is not configured for this account.", 400
    if not consume_recovery_code(user, code):
        return None, "Invalid or already-used recovery code.", 400

    # A1 owns auth_service. Reusing its completion path preserves lock-counter
    # resets, login audits, timestamps, and security notifications exactly.
    from .services.auth_service import _complete_login

    return _complete_login(user)


def mint_admin_recovery_grant(
    username: str, *, duration_minutes: int = 10
) -> tuple[User, str, datetime]:
    """Mint a short-lived CLI token for an active, interactive administrator."""
    duration_minutes = max(1, min(30, int(duration_minutes)))
    user = User.query.filter_by(username=(username or "").strip()).first()
    if (
        user is None
        or not user.is_active
        or user.is_service_account
        or not user.interactive_login_enabled
        or not is_admin(user)
    ):
        raise ValueError("Active interactive administrator not found.")

    now = _utcnow()
    AdminRecoveryGrant.query.filter(
        AdminRecoveryGrant.user_id == user.id,
        AdminRecoveryGrant.used_at.is_(None),
    ).update({AdminRecoveryGrant.used_at: now}, synchronize_session=False)
    raw_token = secrets.token_urlsafe(48)
    expires_at = now + timedelta(minutes=duration_minutes)
    db.session.add(
        AdminRecoveryGrant(
            user_id=user.id,
            token_hash=_hash(raw_token),
            expires_at=expires_at,
        )
    )
    log_audit(
        "admin_recovery_grant_created",
        target_type="user",
        target_id=user.id,
        details={"expiresAt": expires_at.isoformat()},
        commit=False,
    )
    db.session.commit()
    return user, raw_token, expires_at


def consume_admin_recovery_grant(username: str, raw_token: str) -> User | None:
    """Consume a CLI grant, reset MFA state, unlock, and revoke all sessions."""
    user = User.query.filter_by(username=(username or "").strip()).first()
    token_hash = _hash(raw_token or "")
    now = _utcnow()
    grant = None
    if user is not None:
        grant = AdminRecoveryGrant.query.filter_by(
            user_id=user.id, token_hash=token_hash
        ).first()
    consumed = 0
    if (
        user is not None
        and user.is_active
        and not user.is_service_account
        and user.interactive_login_enabled
        and is_admin(user)
        and grant is not None
    ):
        consumed = AdminRecoveryGrant.query.filter(
            AdminRecoveryGrant.id == grant.id,
            AdminRecoveryGrant.used_at.is_(None),
            AdminRecoveryGrant.expires_at > now,
        ).update({AdminRecoveryGrant.used_at: now}, synchronize_session=False)
    if consumed != 1:
        db.session.rollback()
        log_audit(
            "admin_recovery_grant_rejected",
            target_type="user",
            target_id=user.id if user else None,
            details={"outcome": "rejected"},
        )
        return None

    user.mfa_enabled = False
    user.totp_secret = None
    user.first_login_completed = False
    user.must_change_password = False
    user.mfa_failed_attempts = 0
    user.failed_login_attempts = 0
    user.last_failed_login_at = None
    user.locked_until = None
    user.lock_reason = None
    user.lock_count_24h = 0
    user.requires_admin_unlock = False
    log_audit(
        "admin_recovery_grant_used",
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        details={"outcome": "success", "nextStage": "mfa_setup"},
        commit=False,
    )
    revoke_all_user_sessions(user, "admin_recovery")
    return user
