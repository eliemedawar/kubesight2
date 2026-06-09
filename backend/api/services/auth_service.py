from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..audit import log_audit
from ..auth_utils import create_access_token, current_user_profile
from ..db import db
from ..models import User
from ..passwords import verify_password


def login_user(username: str, password: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Returns (payload, error_message, http_status)."""
    user = User.query.filter_by(username=username).first()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        log_audit(
            "login_failed",
            actor_user_id=user.id if user else None,
            target_type="user",
            target_id=username,
            details={"reason": "invalid_credentials"},
        )
        return None, "Invalid credentials", 401

    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()
    log_audit("login_success", actor=user, target_type="user", target_id=str(user.id))

    token = create_access_token(user)
    return {"token": token, "user": current_user_profile(user)}, None, 200


def profile_for_user(user: User) -> Dict[str, Any]:
    return current_user_profile(user)


def logout_user(user: Optional[User]) -> Dict[str, str]:
    if user:
        log_audit("logout", actor=user, target_type="user", target_id=str(user.id))
    return {"message": "Logged out"}
