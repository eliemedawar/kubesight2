"""Persistence and identity orchestration for the hardened OIDC core."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import Response, has_request_context, request
from sqlalchemy import func

from .audit import log_audit
from .db import db
from .models import Role, User
from .models_auth import OidcAuthorizationRequest, OidcIdentity
from .oidc import (
    OidcConfig,
    OidcConfigurationError,
    OidcPrincipal,
    OidcProtocolError,
    begin_authorization,
    exchange_code,
    fetch_discovery,
    hash_transaction_secret,
    oidc_enabled,
    principal_from_claims,
    validate_id_token,
)
from .passwords import hash_password
from .secret_encryption import decrypt_secret, encrypt_secret
from .session_auth import (
    IssuedBrowserSession,
    auth_cookie_secure,
    issue_browser_session,
)


OIDC_FLOW_COOKIE = "kubesight_oidc_flow"
OIDC_FLOW_COOKIE_PATH = "/api/auth/oidc"
_AUTHORIZATION_LIFETIME = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip() -> str:
    if not has_request_context():
        return ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def oidc_status() -> dict:
    if not oidc_enabled():
        return {"enabled": False}
    try:
        config = OidcConfig.from_environment()
    except OidcConfigurationError:
        return {"enabled": False, "configurationError": True}
    return {"enabled": True, "issuer": config.issuer}


@dataclass(frozen=True)
class StartedOidcLogin:
    authorization_url: str
    browser_binding: str
    expires_at: datetime


@dataclass(frozen=True)
class CompletedOidcLogin:
    user: User
    issued_session: IssuedBrowserSession
    return_to: str
    provisioned: bool


def _configured() -> OidcConfig:
    if not oidc_enabled():
        raise OidcConfigurationError("OIDC is not enabled.")
    return OidcConfig.from_environment()


def start_oidc_login(*, return_to: str | None = None) -> StartedOidcLogin:
    config = _configured()
    discovery = fetch_discovery(config)
    transaction = begin_authorization(
        config, discovery, return_to=return_to
    )
    now = _utcnow()
    expires_at = now + _AUTHORIZATION_LIFETIME
    OidcAuthorizationRequest.query.filter(
        OidcAuthorizationRequest.expires_at < now - timedelta(days=1)
    ).delete(synchronize_session=False)
    row = OidcAuthorizationRequest(
        state_hash=hash_transaction_secret(transaction.state),
        nonce_hash=hash_transaction_secret(transaction.nonce),
        browser_binding_hash=hash_transaction_secret(
            transaction.browser_binding
        ),
        code_verifier_cipher=encrypt_secret(transaction.code_verifier),
        issuer=config.issuer,
        redirect_uri=config.redirect_uri,
        return_to=transaction.return_to,
        expires_at=expires_at,
    )
    db.session.add(row)
    db.session.flush()
    log_audit(
        "oidc_login_started",
        target_type="oidc_authorization_request",
        target_id=row.id,
        details={"issuer": config.issuer},
        commit=False,
    )
    db.session.commit()
    return StartedOidcLogin(
        authorization_url=transaction.authorization_url,
        browser_binding=transaction.browser_binding,
        expires_at=expires_at,
    )


def _usable_user(user: User | None) -> bool:
    return bool(
        user
        and user.is_active
        and not user.is_service_account
        and user.interactive_login_enabled
    )


def _unique_username(candidate: str, subject: str) -> str:
    base = candidate[:120] or f"oidc-{hash_transaction_secret(subject)[:12]}"
    if User.query.filter(func.lower(User.username) == base.lower()).first() is None:
        return base
    suffix = hash_transaction_secret(subject)[:12]
    prefixed = f"{base[:107]}-{suffix}"
    if User.query.filter(func.lower(User.username) == prefixed.lower()).first() is None:
        return prefixed
    for attempt in range(2, 100):
        numbered = f"{base[:103]}-{suffix}-{attempt}"
        if User.query.filter(
            func.lower(User.username) == numbered.lower()
        ).first() is None:
            return numbered
    raise OidcProtocolError("Unable to allocate a unique OIDC username.")


def _resolve_identity(
    config: OidcConfig, principal: OidcPrincipal
) -> tuple[User, bool]:
    role = Role.query.filter_by(name=principal.role_name).first()
    if role is None:
        raise OidcProtocolError("OIDC mapped role does not exist.")

    identity = OidcIdentity.query.filter_by(
        issuer=config.issuer, subject=principal.subject
    ).first()
    if identity is not None:
        user = db.session.get(User, identity.user_id)
        if not _usable_user(user):
            raise OidcProtocolError("OIDC identity is not available.")
        old_role_id = user.role_id
        user.role_id = role.id
        identity.email = principal.email
        identity.last_login_at = _utcnow()
        user.email = principal.email
        if principal.full_name:
            user.full_name = principal.full_name
        if old_role_id != role.id:
            log_audit(
                "oidc_role_synchronized",
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
                details={"role": role.name},
                commit=False,
            )
        return user, False

    existing = User.query.filter(
        func.lower(User.email) == principal.email.lower()
    ).first()
    if existing is not None:
        if not config.link_by_email:
            raise OidcProtocolError(
                "OIDC email matches an existing account; explicit linking is required."
            )
        if not _usable_user(existing):
            raise OidcProtocolError("OIDC identity is not available.")
        user = existing
        user.role_id = role.id
        log_audit(
            "oidc_identity_linked",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            details={"issuer": config.issuer},
            commit=False,
        )
        provisioned = False
    else:
        if not config.auto_provision:
            raise OidcProtocolError("OIDC account auto-provisioning is disabled.")
        user = User(
            username=_unique_username(principal.username, principal.subject),
            email=principal.email,
            full_name=principal.full_name,
            password_hash=hash_password(secrets.token_urlsafe(64)),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
            temporary_password_used=True,
            mfa_enabled=False,
            first_login_completed=True,
            is_service_account=False,
            interactive_login_enabled=True,
        )
        db.session.add(user)
        db.session.flush()
        log_audit(
            "oidc_user_provisioned",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            details={"issuer": config.issuer, "role": role.name},
            commit=False,
        )
        provisioned = True

    db.session.add(
        OidcIdentity(
            issuer=config.issuer,
            subject=principal.subject,
            user_id=user.id,
            email=principal.email,
        )
    )
    return user, provisioned


def complete_oidc_login(
    *, state: str, code: str, browser_binding: str
) -> CompletedOidcLogin:
    config = _configured()
    now = _utcnow()
    row = OidcAuthorizationRequest.query.filter_by(
        state_hash=hash_transaction_secret(state or "")
    ).first()
    binding_hash = hash_transaction_secret(browser_binding or "")
    if (
        row is None
        or not row.is_active(now)
        or not hmac.compare_digest(row.browser_binding_hash, binding_hash)
        or row.issuer != config.issuer
        or row.redirect_uri != config.redirect_uri
    ):
        raise OidcProtocolError("OIDC authorization request is invalid or expired.")

    consumed = OidcAuthorizationRequest.query.filter(
        OidcAuthorizationRequest.id == row.id,
        OidcAuthorizationRequest.consumed_at.is_(None),
        OidcAuthorizationRequest.expires_at > now,
        OidcAuthorizationRequest.browser_binding_hash == binding_hash,
    ).update({OidcAuthorizationRequest.consumed_at: now}, synchronize_session=False)
    if consumed != 1:
        db.session.rollback()
        raise OidcProtocolError("OIDC authorization request was already consumed.")
    db.session.commit()

    try:
        verifier = decrypt_secret(row.code_verifier_cipher)
        if not verifier:
            raise OidcProtocolError("OIDC PKCE verifier is unavailable.")
        discovery = fetch_discovery(config)
        tokens = exchange_code(
            config,
            discovery,
            code=code,
            code_verifier=verifier,
        )
        claims = validate_id_token(
            config,
            discovery,
            id_token=str(tokens["id_token"]),
            expected_nonce_hash=row.nonce_hash,
        )
        principal = principal_from_claims(config, claims)
        user, provisioned = _resolve_identity(config, principal)
        user.last_login_at = now
        user.last_login_ip = _client_ip()
        log_audit(
            "oidc_login_succeeded",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            details={"issuer": config.issuer, "provisioned": provisioned},
            commit=False,
        )
        issued = issue_browser_session(user)
        return CompletedOidcLogin(
            user=user,
            issued_session=issued,
            return_to=row.return_to,
            provisioned=provisioned,
        )
    except (OidcConfigurationError, OidcProtocolError) as exc:
        db.session.rollback()
        log_audit(
            "oidc_login_failed",
            target_type="oidc_authorization_request",
            target_id=row.id,
            details={"errorType": type(exc).__name__},
        )
        raise


def set_oidc_flow_cookie(response: Response, browser_binding: str) -> None:
    response.set_cookie(
        OIDC_FLOW_COOKIE,
        browser_binding,
        max_age=int(_AUTHORIZATION_LIFETIME.total_seconds()),
        secure=auth_cookie_secure(),
        httponly=True,
        samesite="Lax",
        path=OIDC_FLOW_COOKIE_PATH,
    )


def clear_oidc_flow_cookie(response: Response) -> None:
    response.delete_cookie(OIDC_FLOW_COOKIE, path=OIDC_FLOW_COOKIE_PATH)
