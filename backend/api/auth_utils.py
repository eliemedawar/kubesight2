from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Dict, Optional

import jwt
from flask import g

from .models import ApiToken, User
from .serializers import user_to_dict


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if secret:
        return secret
    return os.getenv("FLASK_SECRET_KEY", "kubesight-dev-secret-change-me")


def jwt_expiry_hours() -> int:
    try:
        return max(1, int(os.getenv("JWT_EXPIRY_HOURS", "8")))
    except ValueError:
        return 8


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "iat": now,
        "exp": now + timedelta(hours=jwt_expiry_hours()),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


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


def load_user_from_token(token: str) -> Optional[User]:
    payload = decode_access_token(token)
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
    return user


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
        if hasattr(g, "current_user"):
            return g.current_user
        g.current_user = None
        return None

    cached_token = getattr(g, "auth_token", None)
    if hasattr(g, "current_user") and cached_token == token:
        return g.current_user

    if token.startswith("ksa_"):
        user = _load_user_from_api_token(token)
    else:
        user = load_user_from_token(token)

    g.current_user = user
    g.auth_token = token
    return user


def auth_required_enabled() -> bool:
    value = os.getenv("AUTH_REQUIRED", "true").strip().lower()
    return value not in ("false", "0", "no", "off")


def current_user_profile(user: User) -> Dict[str, Any]:
    return user_to_dict(user, include_access=True)
