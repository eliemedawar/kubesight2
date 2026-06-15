"""API token management — create, list, and revoke long-lived service tokens."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from hashlib import sha256
from typing import Optional

from flask import Blueprint, request

from ..access_engine import is_admin
from ..audit import log_audit
from ..auth_utils import get_current_user
from ..db import db
from ..models import ApiToken, User
from ..response import error_response, success_response

api_tokens_bp = Blueprint("api_tokens", __name__, url_prefix="/api/auth")


def _require_auth():
    user = get_current_user()
    if not user:
        return None, error_response("Authentication required", 401)
    return user, None


def _token_to_dict(token: ApiToken) -> dict:
    return {
        "id": token.id,
        "name": token.name,
        "prefix": token.token_prefix,
        "is_active": token.is_active,
        "user_id": token.user_id,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "created_at": token.created_at.isoformat() if token.created_at else None,
    }


# ---------------------------------------------------------------------------
# POST /api/auth/tokens  — create a new API token
# ---------------------------------------------------------------------------

@api_tokens_bp.route("/tokens", methods=["POST"])
def create_token():
    user, err = _require_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return error_response("Token name is required")

    target_user_id = body.get("user_id")
    if target_user_id is not None and int(target_user_id) != user.id:
        if not is_admin(user):
            return error_response("Admin permission required to create tokens for other users", 403)
        target_user = User.query.get(int(target_user_id))
        if not target_user:
            return error_response("Target user not found", 404)
    else:
        target_user = user

    expires_at: Optional[datetime] = None
    if body.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return error_response("Invalid expires_at — use ISO 8601 format (e.g. 2026-12-31T00:00:00Z)")

    raw_token = "ksa_" + secrets.token_hex(20)  # 44 chars total
    token_hash = sha256(raw_token.encode()).hexdigest()
    token_prefix = raw_token[:12]  # "ksa_XXXXXXXX"

    api_token = ApiToken(
        user_id=target_user.id,
        name=name,
        token_prefix=token_prefix,
        token_hash=token_hash,
        expires_at=expires_at,
        is_active=True,
    )
    db.session.add(api_token)
    db.session.flush()

    log_audit(
        "api_token.create",
        actor=user,
        target_type="api_token",
        target_id=str(api_token.id),
        details={
            "name": name,
            "for_user_id": target_user.id,
            "for_username": target_user.username,
        },
    )

    result = _token_to_dict(api_token)
    result["token"] = raw_token  # shown only once at creation
    return success_response(result, 201)


# ---------------------------------------------------------------------------
# GET /api/auth/tokens  — list tokens (own, or any user's for admins)
# ---------------------------------------------------------------------------

@api_tokens_bp.route("/tokens", methods=["GET"])
def list_tokens():
    user, err = _require_auth()
    if err:
        return err

    target_user_id = request.args.get("user_id")
    if target_user_id is not None:
        if not is_admin(user):
            return error_response("Admin permission required to view other users' tokens", 403)
        tokens = (
            ApiToken.query
            .filter_by(user_id=int(target_user_id))
            .order_by(ApiToken.created_at.desc())
            .all()
        )
    else:
        tokens = (
            ApiToken.query
            .filter_by(user_id=user.id)
            .order_by(ApiToken.created_at.desc())
            .all()
        )

    return success_response({"tokens": [_token_to_dict(t) for t in tokens], "total": len(tokens)})


# ---------------------------------------------------------------------------
# DELETE /api/auth/tokens/<id>  — revoke a token
# ---------------------------------------------------------------------------

@api_tokens_bp.route("/tokens/<int:token_id>", methods=["DELETE"])
def revoke_token(token_id: int):
    user, err = _require_auth()
    if err:
        return err

    token = ApiToken.query.get(token_id)
    if not token:
        return error_response("Token not found", 404)
    if token.user_id != user.id and not is_admin(user):
        return error_response("Permission denied", 403)

    token.is_active = False
    log_audit(
        "api_token.revoke",
        actor=user,
        target_type="api_token",
        target_id=str(token_id),
        details={"name": token.name, "owner_user_id": token.user_id},
    )
    db.session.commit()

    return success_response({"message": "Token revoked", "id": token_id})
