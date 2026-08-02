"""Cookie-session issuance, refresh rotation, revocation, and CSRF checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import Response, current_app, g, request

from .audit import log_audit
from .auth_utils import (
    PURPOSE_ACCESS,
    create_access_token,
    decode_access_token,
    load_user_from_token,
)
from .db import db
from .models import User
from .models_auth import AuthRefreshToken, AuthSession


ACCESS_COOKIE = "kubesight_access"
REFRESH_COOKIE = "kubesight_refresh"
INTERIM_COOKIE = "kubesight_interim"
CSRF_COOKIE = "kubesight_csrf"
CSRF_HEADER = "X-CSRF-Token"

_AUTH_COOKIE_PATH = "/api/auth"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def access_session_minutes() -> int:
    return _positive_int("AUTH_ACCESS_SESSION_MINUTES", 15)


def refresh_session_days() -> int:
    return _positive_int("AUTH_REFRESH_SESSION_DAYS", 30)


def auth_cookie_secure() -> bool:
    raw = os.getenv("AUTH_COOKIE_SECURE", "true").strip().lower()
    configured = raw not in {"0", "false", "no", "off"}
    if os.getenv("KUBESIGHT_ENV", "").strip().lower() == "production":
        return True
    return configured


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def _user_agent() -> str:
    return request.headers.get("User-Agent", "")[:512]


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


@dataclass(frozen=True)
class IssuedBrowserSession:
    session_id: str
    access_token: str
    refresh_token: str


def _new_refresh_record(
    session_id: str, raw_token: str, expires_at: datetime
) -> AuthRefreshToken:
    return AuthRefreshToken(
        session_id=session_id,
        token_hash=_token_hash(raw_token),
        expires_at=expires_at,
    )


def issue_browser_session(user: User) -> IssuedBrowserSession:
    """Create a server-side session and its first single-use refresh token."""
    now = _utcnow()
    refresh_expires_at = now + timedelta(days=refresh_session_days())
    session = AuthSession(
        user_id=user.id,
        refresh_expires_at=refresh_expires_at,
        ip_address=_client_ip(),
        user_agent=_user_agent(),
    )
    db.session.add(session)
    db.session.flush()

    raw_refresh = _new_refresh_token()
    db.session.add(
        _new_refresh_record(session.id, raw_refresh, refresh_expires_at)
    )
    log_audit(
        "session_created",
        actor=user,
        target_type="auth_session",
        target_id=session.id,
        details={
            "userAgent": session.user_agent,
            "ip": session.ip_address,
            "refreshExpiresAt": refresh_expires_at.isoformat(),
        },
        commit=False,
    )
    db.session.commit()

    access_token = create_access_token(
        user,
        session_id=session.id,
        expiry_minutes=access_session_minutes(),
    )
    return IssuedBrowserSession(session.id, access_token, raw_refresh)


def _revoke_family(
    session: AuthSession,
    reason: str,
    *,
    actor_user_id: int | None = None,
    audit_action: str = "session_revoked",
) -> None:
    now = _utcnow()
    if session.revoked_at is None:
        session.revoked_at = now
        session.revoke_reason = reason
    AuthRefreshToken.query.filter(
        AuthRefreshToken.session_id == session.id,
        AuthRefreshToken.revoked_at.is_(None),
    ).update({AuthRefreshToken.revoked_at: now}, synchronize_session=False)
    log_audit(
        audit_action,
        actor_user_id=actor_user_id or session.user_id,
        target_type="auth_session",
        target_id=session.id,
        details={"reason": reason},
        commit=False,
    )
    db.session.commit()


def rotate_refresh_token(
    raw_token: str,
) -> tuple[User | None, IssuedBrowserSession | None, str | None]:
    """Consume one refresh token and replace it atomically.

    Presenting a consumed token is reuse, not a generic authentication failure;
    it revokes the entire session family before returning.
    """
    if not raw_token:
        return None, None, "Refresh session is missing."

    token_record = AuthRefreshToken.query.filter_by(
        token_hash=_token_hash(raw_token)
    ).first()
    if token_record is None:
        return None, None, "Refresh session is invalid or expired."

    session = db.session.get(AuthSession, token_record.session_id)
    if session is None:
        return None, None, "Refresh session is invalid or expired."

    if token_record.used_at is not None or token_record.revoked_at is not None:
        _revoke_family(
            session,
            "refresh_token_reuse",
            audit_action="refresh_token_reuse_detected",
        )
        return None, None, "Refresh token reuse detected; session revoked."

    now = _utcnow()
    token_expires = _as_utc(token_record.expires_at)
    if not session.is_active(now) or not token_expires or token_expires <= now:
        _revoke_family(session, "expired")
        return None, None, "Refresh session is invalid or expired."

    user = db.session.get(User, session.user_id)
    if not user or not user.is_active or not user.first_login_completed:
        _revoke_family(session, "user_unavailable")
        return None, None, "Refresh session is invalid or expired."

    consumed = AuthRefreshToken.query.filter(
        AuthRefreshToken.id == token_record.id,
        AuthRefreshToken.used_at.is_(None),
        AuthRefreshToken.revoked_at.is_(None),
    ).update({AuthRefreshToken.used_at: now}, synchronize_session=False)
    if consumed != 1:
        db.session.rollback()
        session = db.session.get(AuthSession, token_record.session_id)
        if session is not None:
            _revoke_family(
                session,
                "refresh_token_reuse",
                audit_action="refresh_token_reuse_detected",
            )
        return None, None, "Refresh token reuse detected; session revoked."

    raw_replacement = _new_refresh_token()
    replacement = _new_refresh_record(
        session.id, raw_replacement, session.refresh_expires_at
    )
    db.session.add(replacement)
    db.session.flush()
    token_record.replaced_by_token_id = replacement.id
    session.last_seen_at = now
    session.ip_address = _client_ip()
    session.user_agent = _user_agent()
    log_audit(
        "session_refreshed",
        actor=user,
        target_type="auth_session",
        target_id=session.id,
        commit=False,
    )
    db.session.commit()

    access_token = create_access_token(
        user,
        session_id=session.id,
        expiry_minutes=access_session_minutes(),
    )
    return (
        user,
        IssuedBrowserSession(session.id, access_token, raw_replacement),
        None,
    )


def load_user_from_cookie_token(raw_token: str) -> User | None:
    payload = decode_access_token(raw_token)
    if not payload or payload.get("purpose", PURPOSE_ACCESS) != PURPOSE_ACCESS:
        return None
    session_id = payload.get("sid")
    if not session_id:
        return None
    session = db.session.get(AuthSession, str(session_id))
    if not session or not session.is_active():
        return None
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        return None
    if user_id != session.user_id:
        return None
    user = db.session.get(User, user_id)
    if not user or not user.is_active:
        return None

    now = _utcnow()
    last_seen = _as_utc(session.last_seen_at)
    if not last_seen or now - last_seen >= timedelta(minutes=1):
        session.last_seen_at = now
        db.session.commit()
    g.auth_session_id = session.id
    return user


def _csrf_secret() -> bytes:
    value = str(current_app.config.get("JWT_SECRET_KEY") or "")
    return value.encode("utf-8")


def issue_csrf_token() -> str:
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(
        _csrf_secret(), nonce.encode("ascii"), hashlib.sha256
    ).digest()
    encoded = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{nonce}.{encoded}"


def csrf_token_valid(token: str) -> bool:
    try:
        nonce, supplied = token.rsplit(".", 1)
    except ValueError:
        return False
    signature = hmac.new(
        _csrf_secret(), nonce.encode("ascii"), hashlib.sha256
    ).digest()
    expected = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return hmac.compare_digest(supplied, expected)


def csrf_violation() -> str | None:
    if request.method in _SAFE_METHODS:
        return None
    if request.path in {"/api/auth/login", "/api/auth/admin-recovery"}:
        return None
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return None
    cookie_authenticated = any(
        request.cookies.get(name)
        for name in (ACCESS_COOKIE, REFRESH_COOKIE, INTERIM_COOKIE)
    )
    if not cookie_authenticated:
        return None
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    header_token = request.headers.get(CSRF_HEADER, "")
    if (
        not cookie_token
        or not header_token
        or not hmac.compare_digest(cookie_token, header_token)
        or not csrf_token_valid(cookie_token)
    ):
        return f"{CSRF_HEADER} must match the signed {CSRF_COOKIE} cookie."
    return None


def _set_cookie(
    response: Response,
    name: str,
    value: str,
    *,
    max_age: int,
    httponly: bool,
    path: str,
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        secure=auth_cookie_secure(),
        httponly=httponly,
        samesite="Lax",
        path=path,
    )


def set_csrf_cookie(response: Response, token: str) -> None:
    _set_cookie(
        response,
        CSRF_COOKIE,
        token,
        max_age=refresh_session_days() * 86400,
        httponly=False,
        path="/",
    )


def set_interim_cookie(response: Response, token: str) -> None:
    _set_cookie(
        response,
        INTERIM_COOKIE,
        token,
        max_age=30 * 60,
        httponly=True,
        path=_AUTH_COOKIE_PATH,
    )
    set_csrf_cookie(response, issue_csrf_token())


def set_session_cookies(
    response: Response, issued: IssuedBrowserSession
) -> None:
    _set_cookie(
        response,
        ACCESS_COOKIE,
        issued.access_token,
        max_age=access_session_minutes() * 60,
        httponly=True,
        path="/",
    )
    _set_cookie(
        response,
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=refresh_session_days() * 86400,
        httponly=True,
        path=_AUTH_COOKIE_PATH,
    )
    response.delete_cookie(INTERIM_COOKIE, path=_AUTH_COOKIE_PATH)
    set_csrf_cookie(response, issue_csrf_token())


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=_AUTH_COOKIE_PATH)
    response.delete_cookie(INTERIM_COOKIE, path=_AUTH_COOKIE_PATH)
    response.delete_cookie(CSRF_COOKIE, path="/")


def apply_login_cookies(response: Response, payload: dict | None) -> None:
    if not payload:
        return
    legacy_access_token = payload.get("token")
    if payload.get("stage") == "authenticated" and legacy_access_token:
        user = load_user_from_token(legacy_access_token)
        if user:
            set_session_cookies(response, issue_browser_session(user))
        return
    interim_token = payload.get("onboardingToken") or payload.get("mfaToken")
    if interim_token:
        set_interim_cookie(response, interim_token)


def current_session_id() -> str | None:
    cached = getattr(g, "auth_session_id", None)
    if cached:
        return cached
    raw_access = request.cookies.get(ACCESS_COOKIE, "")
    payload = decode_access_token(raw_access) if raw_access else None
    return str(payload.get("sid")) if payload and payload.get("sid") else None


def list_user_sessions(user: User) -> list[dict]:
    current = current_session_id()
    sessions = (
        AuthSession.query.filter_by(user_id=user.id)
        .order_by(AuthSession.created_at.desc())
        .all()
    )
    return [session.to_dict(current_session_id=current) for session in sessions]


def revoke_user_session(user: User, session_id: str, reason: str) -> bool:
    session = AuthSession.query.filter_by(id=session_id, user_id=user.id).first()
    if not session:
        return False
    _revoke_family(session, reason, actor_user_id=user.id)
    return True


def revoke_all_user_sessions(user: User, reason: str) -> int:
    sessions = AuthSession.query.filter_by(user_id=user.id, revoked_at=None).all()
    for session in sessions:
        now = _utcnow()
        session.revoked_at = now
        session.revoke_reason = reason
        AuthRefreshToken.query.filter(
            AuthRefreshToken.session_id == session.id,
            AuthRefreshToken.revoked_at.is_(None),
        ).update({AuthRefreshToken.revoked_at: now}, synchronize_session=False)
    log_audit(
        "all_sessions_revoked",
        actor=user,
        target_type="user",
        target_id=user.id,
        details={"reason": reason, "count": len(sessions)},
        commit=False,
    )
    db.session.commit()
    return len(sessions)


def revoke_request_session(user: User | None) -> None:
    raw_refresh = request.cookies.get(REFRESH_COOKIE, "")
    token_record = None
    if raw_refresh:
        token_record = AuthRefreshToken.query.filter_by(
            token_hash=_token_hash(raw_refresh)
        ).first()
    session_id = token_record.session_id if token_record else current_session_id()
    if session_id:
        session = db.session.get(AuthSession, session_id)
        if session and (user is None or session.user_id == user.id):
            _revoke_family(
                session,
                "logout",
                actor_user_id=user.id if user else session.user_id,
            )
