"""Server-side browser sessions and refresh-token rotation records."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from .db import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return secrets.token_hex(16)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class AuthSession(db.Model):
    """One revocable browser session and refresh-token family."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        db.Index("ix_auth_sessions_user_revoked", "user_id", "revoked_at"),
        db.Index("ix_auth_sessions_refresh_expiry", "refresh_expires_at"),
    )

    id = db.Column(db.String(32), primary_key=True, default=_new_id)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_seen_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    refresh_expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoke_reason = db.Column(db.String(64), nullable=True)
    ip_address = db.Column(db.String(64), nullable=False, default="")
    user_agent = db.Column(db.String(512), nullable=False, default="")

    def is_active(self, now: datetime | None = None) -> bool:
        current = now or _utcnow()
        expires_at = _as_utc(self.refresh_expires_at)
        return self.revoked_at is None and bool(expires_at and expires_at > current)

    def to_dict(self, *, current_session_id: str | None = None) -> dict:
        return {
            "id": self.id,
            "createdAt": self.created_at.isoformat(),
            "lastSeenAt": self.last_seen_at.isoformat(),
            "expiresAt": self.refresh_expires_at.isoformat(),
            "revokedAt": self.revoked_at.isoformat() if self.revoked_at else None,
            "ipAddress": self.ip_address,
            "userAgent": self.user_agent,
            "current": self.id == current_session_id,
        }


class AuthRefreshToken(db.Model):
    """Hashed single-use token in an ``AuthSession`` refresh family."""

    __tablename__ = "auth_refresh_tokens"
    __table_args__ = (
        db.Index(
            "ix_auth_refresh_tokens_session_used",
            "session_id",
            "used_at",
        ),
    )

    id = db.Column(db.String(32), primary_key=True, default=_new_id)
    session_id = db.Column(
        db.String(32),
        db.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    replaced_by_token_id = db.Column(
        db.String(32),
        db.ForeignKey("auth_refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )


class OidcIdentity(db.Model):
    """Stable binding between an OIDC issuer/subject and one local user."""

    __tablename__ = "oidc_identities"
    __table_args__ = (
        db.UniqueConstraint(
            "issuer", "subject", name="uq_oidc_identity_issuer_subject"
        ),
    )

    id = db.Column(db.String(32), primary_key=True, default=_new_id)
    issuer = db.Column(db.String(512), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    email = db.Column(db.String(255), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_login_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )


class OidcAuthorizationRequest(db.Model):
    """One-time state, nonce, PKCE, and browser binding for an OIDC login."""

    __tablename__ = "oidc_authorization_requests"
    __table_args__ = (
        db.Index(
            "ix_oidc_authorization_requests_expiry_consumed",
            "expires_at",
            "consumed_at",
        ),
    )

    id = db.Column(db.String(32), primary_key=True, default=_new_id)
    state_hash = db.Column(
        db.String(64), nullable=False, unique=True, index=True
    )
    nonce_hash = db.Column(db.String(64), nullable=False)
    browser_binding_hash = db.Column(db.String(64), nullable=False)
    code_verifier_cipher = db.Column(db.Text, nullable=False)
    issuer = db.Column(db.String(512), nullable=False)
    redirect_uri = db.Column(db.String(2048), nullable=False)
    return_to = db.Column(db.String(2048), nullable=False, default="/")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    consumed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def is_active(self, now: datetime | None = None) -> bool:
        current = now or _utcnow()
        expires_at = _as_utc(self.expires_at)
        return self.consumed_at is None and bool(
            expires_at and expires_at > current
        )


class MfaRecoveryCode(db.Model):
    """Hash-only, single-use MFA recovery code for a user."""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (
        db.Index("ix_mfa_recovery_codes_user_used", "user_id", "used_at"),
    )

    id = db.Column(db.String(32), primary_key=True, default=_new_id)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash = db.Column(
        db.String(64), nullable=False, unique=True, index=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)


class AdminRecoveryGrant(db.Model):
    """Short-lived, hash-only break-glass grant minted by the local CLI."""

    __tablename__ = "admin_recovery_grants"
    __table_args__ = (
        db.Index(
            "ix_admin_recovery_grants_user_expiry",
            "user_id",
            "expires_at",
        ),
    )

    id = db.Column(db.String(32), primary_key=True, default=_new_id)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = db.Column(
        db.String(64), nullable=False, unique=True, index=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def is_active(self, now: datetime | None = None) -> bool:
        current = now or _utcnow()
        expires_at = _as_utc(self.expires_at)
        return self.used_at is None and bool(expires_at and expires_at > current)
