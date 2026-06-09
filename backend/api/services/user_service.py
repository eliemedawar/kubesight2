from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..db import db
from ..models import Role, User
from ..passwords import hash_password
from ..serializers import (
    apply_user_access,
    parse_cluster_access_payload,
    parse_namespace_access_payload,
    user_list_item,
    user_to_dict,
)


def active_admin_count() -> int:
    admin_role = Role.query.filter_by(name="admin").first()
    if not admin_role:
        return 0
    return User.query.filter_by(role_id=admin_role.id, is_active=True).count()


def list_users() -> Dict[str, Any]:
    users = User.query.order_by(User.username.asc()).all()
    return {"items": [user_list_item(u) for u in users], "count": len(users)}


def get_user(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    return user_to_dict(user, include_access=True), None, 200


def create_user(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    username = (payload.get("username") or "").strip()
    email = (payload.get("email") or "").strip()
    full_name = (payload.get("fullName") or "").strip()
    password = payload.get("password") or ""
    role_id = payload.get("roleId")

    if not username or not password or not role_id:
        return None, "username, password, and roleId are required", 400

    if User.query.filter_by(username=username).first():
        return None, "Username already exists", 409

    role = Role.query.get(role_id)
    if not role:
        return None, "Role not found", 404

    user = User(
        username=username,
        email=email or f"{username}@kubesight.local",
        full_name=full_name or username,
        password_hash=hash_password(password),
        role_id=role.id,
        is_active=True,
    )
    db.session.add(user)
    db.session.flush()

    access_rules = payload.get("accessRules")
    if access_rules is not None:
        apply_user_access(user, [], [], access_rules=access_rules)
    else:
        cluster_ids = parse_cluster_access_payload(payload.get("clusterAccess"))
        namespace_rows = parse_namespace_access_payload(payload.get("namespaceAccess"))
        apply_user_access(user, cluster_ids, namespace_rows)

    db.session.commit()
    log_audit(
        "user_created",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username},
    )
    return user_to_dict(user, include_access=True), None, 201


def disable_user(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..access import is_admin

    user = User.query.get(user_id)
    if not user:
        return None, "User not found", 404
    if not user.is_active:
        return {"id": user.id, "isActive": False}, None, 200

    if is_admin(user) and active_admin_count() <= 1:
        return None, "Cannot disable the last active admin", 400

    user.is_active = False
    db.session.commit()
    log_audit(
        "user_disabled",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username},
    )
    return {"id": user.id, "isActive": False}, None, 200
