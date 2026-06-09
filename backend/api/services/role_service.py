"""Role and permission catalog operations."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func

from ..access_engine import user_has_permission
from ..audit import log_audit
from ..db import db
from ..models import Permission, Role, User
from ..rbac_data import ALL_PERMISSION_KEYS
from ..serializers import role_to_dict

ROLE_MGMT_PERMISSIONS = ("roles:manage", "users:manage")
USER_MGMT_PERMISSIONS = ("users:manage", "users:create", "users:update", "users:disable")
ROLE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


def can_manage_roles(user: User) -> bool:
    return any(user_has_permission(user, key) for key in ROLE_MGMT_PERMISSIONS)


def list_roles() -> Dict[str, Any]:
    roles = Role.query.order_by(Role.name.asc()).all()
    user_counts = dict(
        db.session.query(User.role_id, func.count(User.id)).group_by(User.role_id).all()
    )
    return {
        "items": [role_to_dict(r, user_count=user_counts.get(r.id, 0)) for r in roles],
        "count": len(roles),
    }


def list_permissions() -> Dict[str, Any]:
    perms = Permission.query.order_by(Permission.key.asc()).all()
    items = [{"id": p.id, "key": p.key, "description": p.description} for p in perms]
    return {"items": items, "count": len(items)}


def get_role(role_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    role = Role.query.get(role_id)
    if not role:
        return None, "Role not found", 404
    user_count = User.query.filter_by(role_id=role.id).count()
    return role_to_dict(role, user_count=user_count), None, 200


def _normalize_role_name(name: str) -> str:
    return re.sub(r"\s+", "_", (name or "").strip().lower())


def _validate_role_name(name: str) -> Optional[str]:
    normalized = _normalize_role_name(name)
    if not normalized:
        return "Role name is required"
    if not ROLE_NAME_PATTERN.match(normalized):
        return "Role name must start with a letter and use only lowercase letters, numbers, and underscores"
    return None


def _resolve_permissions(keys: List[str]) -> Tuple[Optional[List[Permission]], Optional[str]]:
    if not keys:
        return None, "At least one permission is required"
    if not isinstance(keys, list):
        return None, "permissions must be a list"
    unique_keys = sorted({str(key).strip() for key in keys if str(key).strip()})
    if not unique_keys:
        return None, "At least one permission is required"
    permissions = Permission.query.filter(Permission.key.in_(unique_keys)).all()
    found_keys = {perm.key for perm in permissions}
    missing = [key for key in unique_keys if key not in found_keys]
    if missing:
        return None, f"Unknown permissions: {', '.join(missing)}"
    return permissions, None


def _actor_retains_management(actor: Optional[User], role: Role, new_keys: Set[str]) -> Optional[str]:
    if not actor or actor.role_id != role.id:
        return None
    if not (set(ROLE_MGMT_PERMISSIONS) & new_keys):
        return "You cannot remove your own permission to manage roles"
    if not (set(USER_MGMT_PERMISSIONS) & new_keys):
        return "You cannot remove your own permission to manage users"
    return None


def create_role(
    payload: Dict[str, Any],
    *,
    actor: Optional[User] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    name_error = _validate_role_name(payload.get("name") or "")
    if name_error:
        return None, name_error, 400

    normalized_name = _normalize_role_name(payload.get("name") or "")
    if Role.query.filter_by(name=normalized_name).first():
        return None, "Role name already exists", 409

    permissions, perm_error = _resolve_permissions(payload.get("permissions") or [])
    if perm_error:
        return None, perm_error, 400

    description = (payload.get("description") or "").strip()
    role = Role(
        name=normalized_name,
        description=description,
        is_system_role=False,
    )
    role.permissions = permissions
    db.session.add(role)
    db.session.commit()

    log_audit(
        "role_created",
        actor=actor,
        target_type="role",
        target_id=str(role.id),
        details={
            "name": role.name,
            "description": role.description,
            "permissions": [perm.key for perm in role.permissions],
        },
    )
    return role_to_dict(role, user_count=0), None, 201


def update_role(
    role_id: int,
    payload: Dict[str, Any],
    *,
    actor: Optional[User] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    role = Role.query.get(role_id)
    if not role:
        return None, "Role not found", 404

    if "name" in payload and not role.is_system_role:
        name_error = _validate_role_name(payload.get("name") or "")
        if name_error:
            return None, name_error, 400
        normalized_name = _normalize_role_name(payload.get("name") or "")
        existing = Role.query.filter_by(name=normalized_name).first()
        if existing and existing.id != role.id:
            return None, "Role name already exists", 409
        role.name = normalized_name
    elif "name" in payload and role.is_system_role:
        requested = _normalize_role_name(payload.get("name") or "")
        if requested != role.name:
            return None, "System role names cannot be changed", 400

    if "description" in payload:
        role.description = (payload.get("description") or "").strip()

    previous_permissions = sorted(perm.key for perm in role.permissions)
    if "permissions" in payload:
        permissions, perm_error = _resolve_permissions(payload.get("permissions") or [])
        if perm_error:
            return None, perm_error, 400
        new_keys = {perm.key for perm in permissions}
        lockout_error = _actor_retains_management(actor, role, new_keys)
        if lockout_error:
            return None, lockout_error, 400
        role.permissions = permissions

    db.session.commit()

    user_count = User.query.filter_by(role_id=role.id).count()
    new_permissions = sorted(perm.key for perm in role.permissions)
    log_audit(
        "role_updated",
        actor=actor,
        target_type="role",
        target_id=str(role.id),
        details={
            "name": role.name,
            "description": role.description,
            "permissions": new_permissions,
            "previousPermissions": previous_permissions if "permissions" in payload else None,
        },
    )
    return role_to_dict(role, user_count=user_count), None, 200


def update_role_permissions(
    role_id: int,
    permission_keys: List[str],
    *,
    actor: Optional[User] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    return update_role(role_id, {"permissions": permission_keys}, actor=actor)


def delete_role(
    role_id: int,
    *,
    actor: Optional[User] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    role = Role.query.get(role_id)
    if not role:
        return None, "Role not found", 404
    if role.is_system_role:
        return None, "System roles cannot be deleted", 400

    user_count = User.query.filter_by(role_id=role.id).count()
    if user_count > 0:
        return None, f"Cannot delete role assigned to {user_count} user(s)", 400

    details = {
        "name": role.name,
        "description": role.description,
        "permissions": [perm.key for perm in role.permissions],
    }
    db.session.delete(role)
    db.session.commit()

    log_audit(
        "role_deleted",
        actor=actor,
        target_type="role",
        target_id=str(role_id),
        details=details,
    )
    return {"id": role_id, "deleted": True}, None, 200
