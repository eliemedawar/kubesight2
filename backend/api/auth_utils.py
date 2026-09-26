from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Dict, Optional

import jwt
from flask import g

from .models import ApiToken, User
from .runtime_config import (
    INSECURE_DEVELOPMENT_KEY,
    InsecureConfigurationError,
    configured_jwt_secret,
    env_flag,
    is_production_env,
)
from .serializers import user_to_dict

_logger = logging.getLogger(__name__)


def _jwt_secret() -> str:
    secret = configured_jwt_secret()
    if secret:
        return secret
    if is_production_env():
        # create_app refuses to boot without one; this covers anything that
        # signs a token before (or without) going through it.
        raise InsecureConfigurationError("JWT_SECRET_KEY must be set in production.")
    # Development only: honour an explicitly-set value even if it is the
    # default literal, then fall back to it.
    return (
        os.getenv("JWT_SECRET_KEY", "").strip()
        or os.getenv("FLASK_SECRET_KEY", "").strip()
        or INSECURE_DEVELOPMENT_KEY
    )


def jwt_expiry_hours() -> int:
    try:
        return max(1, int(os.getenv("JWT_EXPIRY_HOURS", "8")))
    except ValueError:
        return 8


# JWT "purpose" claim values. A full "access" token authorizes protected app
# endpoints; the short-lived "onboarding" and "mfa" tokens authorize ONLY their
# respective first-login / login-MFA endpoints and are rejected everywhere else.
PURPOSE_ACCESS = "access"
PURPOSE_ONBOARDING = "onboarding"
PURPOSE_MFA = "mfa"
# Authorizes ONE file download of ONE named resource, and nothing else. See
# create_download_ticket for why this exists at all.
PURPOSE_DOWNLOAD = "download"

# Interim tokens (onboarding, pending-MFA) are intentionally short-lived — they
# only need to survive a single multi-step setup / challenge.
_INTERIM_TOKEN_MINUTES = 30

# A download ticket only has to survive the gap between the click that mints it
# and the browser starting the transfer. Two minutes is generous for that and
# short enough that a ticket left in a browser history or an access log is not
# worth stealing. It covers the whole transfer, not just the start: the
# credential is checked when the request is made, so a 200MB download that takes
# an hour is unaffected.
_DOWNLOAD_TICKET_SECONDS = 120


def _encode_token(payload: Dict[str, Any]) -> str:
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def _token_version(user: User) -> int:
    return int(getattr(user, "token_version", 0) or 0)


def revoke_user_sessions(user: User) -> None:
    """Invalidate every JWT issued to ``user`` so far (the caller commits).

    Every session, onboarding and MFA token and download ticket carries the
    user's ``token_version`` as its ``ver`` claim, and ``_user_from_payload``
    refuses a token whose ``ver`` is not the current one. Bumping it is the
    revocation: logout, password change or reset, disable, admin lock and MFA
    reset all call this. Long-lived API tokens (``ksa_``) are separate
    credentials with their own revoke, and are deliberately unaffected.
    """
    user.token_version = _token_version(user) + 1


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return _encode_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "purpose": PURPOSE_ACCESS,
            "ver": _token_version(user),
            "iat": now,
            "exp": now + timedelta(hours=jwt_expiry_hours()),
        }
    )


def create_interim_token(user: User, purpose: str) -> str:
    """Mint a short-lived token scoped to the onboarding or MFA-challenge flow."""
    now = datetime.now(timezone.utc)
    return _encode_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "purpose": purpose,
            "ver": _token_version(user),
            "iat": now,
            "exp": now + timedelta(minutes=_INTERIM_TOKEN_MINUTES),
        }
    )


def create_download_ticket(user: User, resource: str) -> str:
    """Mint a token that authorizes downloading ONE resource, briefly.

    Why this exists: a file download is a plain browser navigation, and a
    browser navigation carries no Authorization header. KubeSight's session is a
    bearer token held in JavaScript, not a cookie, so an ``<a href>`` to a
    protected endpoint arrives anonymous and is refused — which is exactly what
    it should do. The alternatives were to buffer the file in memory through
    ``fetch`` (fine for a log, not for a 200MB JAR) or to put a credential the
    browser CAN send into the URL. This is the second, made as small as it can
    be:

    * ``purpose`` is not ``access``, so ``load_user_from_token`` rejects it and
      it can never reach an ordinary endpoint, even as a Bearer header;
    * ``res`` names one resource, checked on use, so a ticket for one artifact
      cannot fetch another;
    * it expires in two minutes;
    * the endpoint it unlocks is read-only, and still checks the user's
      permission when the download is served.

    Not single-use. A resumed or retried transfer re-requests the same URL, and
    the point of this route is that large downloads survive a dropped
    connection.
    """
    now = datetime.now(timezone.utc)
    return _encode_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "purpose": PURPOSE_DOWNLOAD,
            "ver": _token_version(user),
            "res": resource,
            "iat": now,
            "exp": now + timedelta(seconds=_DOWNLOAD_TICKET_SECONDS),
        }
    )


def load_user_for_download(token: str, resource: str) -> Optional[User]:
    """Resolve a download ticket, requiring it to name this exact resource."""
    payload = decode_access_token(token)
    if not payload or payload.get("purpose") != PURPOSE_DOWNLOAD:
        return None
    if payload.get("res") != resource:
        return None
    return _user_from_payload(payload)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def get_bearer_token() -> Optional[str]:
    from flask import request

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header[7:].strip() or None


def _user_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[User]:
    if not payload:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    user = User.query.get(uid)
    if not user or not user.is_active:
        return None
    # A token minted before the last revocation (logout, password change, lock,
    # …) is dead even though its signature and expiry are fine. Tokens from
    # before this claim existed carry no ``ver`` and count as version 0, so an
    # upgrade does not sign everyone out — only the next revocation does.
    try:
        token_version = int(payload.get("ver", 0) or 0)
    except (TypeError, ValueError):
        return None
    if token_version != _token_version(user):
        return None
    return user


def load_user_from_token(token: str) -> Optional[User]:
    """Resolve a full access token to its user.

    Interim onboarding / MFA-challenge tokens carry a different ``purpose`` and
    are deliberately NOT accepted here, so they can never reach a protected
    endpoint even though they are valid JWTs.
    """
    payload = decode_access_token(token)
    if not payload or payload.get("purpose", PURPOSE_ACCESS) != PURPOSE_ACCESS:
        return None
    return _user_from_payload(payload)


def load_user_for_purpose(token: str, purpose: str) -> Optional[User]:
    """Resolve an interim (onboarding / MFA) token, requiring its exact purpose."""
    payload = decode_access_token(token)
    if not payload or payload.get("purpose") != purpose:
        return None
    return _user_from_payload(payload)


def _load_user_from_api_token(raw_token: str) -> Optional[User]:
    from .db import db

    token_hash = sha256(raw_token.encode()).hexdigest()
    api_token = ApiToken.query.filter_by(token_hash=token_hash, is_active=True).first()
    if not api_token:
        return None
    now = datetime.now(timezone.utc)
    if api_token.expires_at and api_token.expires_at.replace(tzinfo=timezone.utc) < now:
        return None
    user = User.query.get(api_token.user_id)
    if not user or not user.is_active:
        return None
    try:
        api_token.last_used_at = now
        db.session.commit()
    except Exception:
        db.session.rollback()
    return user


def get_current_user() -> Optional[User]:
    token = get_bearer_token()
    if not token:
        # Never reuse a user cached by an earlier request. This matters when an
        # outer application context spans requests (tests, CLI orchestration)
        # and is safer than assuming Flask's ``g`` always has request lifetime.
        g.current_user = None
        g.auth_token = None
        return None

    from flask import request

    # The cache is only good for the request that filled it. The same token
    # arriving on a LATER request (an outer app context spanning requests) must
    # be resolved again, or a logout / revocation in between would not apply.
    current_request = request._get_current_object()
    cached_token = getattr(g, "auth_token", None)
    if (
        hasattr(g, "current_user")
        and cached_token == token
        and getattr(g, "auth_request", None) is current_request
    ):
        return g.current_user

    if token.startswith("ksa_"):
        user = _load_user_from_api_token(token)
    else:
        user = load_user_from_token(token)

    g.current_user = user
    g.auth_token = token
    g.auth_request = current_request
    return user


_auth_kill_switch_warned = False


def auth_required_enabled() -> bool:
    """Whether protected endpoints demand a credential.

    ``AUTH_REQUIRED=false`` is a local-debugging switch. In production it is
    ignored (and ``create_app`` refuses to boot with it set), so no environment
    typo can turn every endpoint anonymous.
    """
    global _auth_kill_switch_warned
    if env_flag("AUTH_REQUIRED", default=True):
        return True
    if is_production_env():
        if not _auth_kill_switch_warned:
            _auth_kill_switch_warned = True
            _logger.error("AUTH_REQUIRED=false is ignored in production; authentication stays on.")
        return True
    return False


def current_user_profile(user: User) -> Dict[str, Any]:
    return user_to_dict(user, include_access=True)
