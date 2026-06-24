from __future__ import annotations

from flask import Blueprint, request

from ..access import is_admin
from ..audit import log_audit
from ..db import db
from ..auth_utils import get_current_user
from ..decorators import require_any_permission, require_permission
from ..models import Role, User
from ..passwords import hash_password
from ..response import error_response, success_response
from ..serializers import (
    apply_user_access,
    parse_cluster_access_payload,
    parse_namespace_access_payload,
    user_to_dict,
)
from ..services import user_service

users_bp = Blueprint("users", __name__, url_prefix="/api/users")


@users_bp.route("", methods=["GET"])
@require_permission("users:view")
def list_users():
    return success_response(user_service.list_users())


@users_bp.route("", methods=["POST"])
@require_permission("users:create")
def create_user():
    payload = request.get_json(silent=True) or {}
    data, error, status = user_service.create_user(payload)
    if error:
        return error_response(error, status)
    return success_response(data, status)


@users_bp.route("/<int:user_id>", methods=["GET"])
@require_permission("users:view")
def get_user(user_id: int):
    data, error, status = user_service.get_user(user_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@users_bp.route("/<int:user_id>", methods=["PUT"])
@require_permission("users:update")
def update_user(user_id: int):
    user = User.query.get(user_id)
    if not user:
        return error_response("User not found", 404)

    payload = request.get_json(silent=True) or {}
    if "fullName" in payload:
        user.full_name = (payload.get("fullName") or "").strip() or user.full_name
    if "email" in payload:
        user.email = (payload.get("email") or "").strip() or user.email
    actor = get_current_user()
    previous_role_id = user.role_id
    previous_role_name = user.role.name if user.role else None
    if "roleId" in payload:
        role = Role.query.get(payload.get("roleId"))
        if not role:
            return error_response("Role not found", 404)
        if actor and actor.id == user.id and user.role_id != role.id:
            from ..serializers import role_has_full_access
            from ..services.role_service import ROLE_MGMT_PERMISSIONS, USER_MGMT_PERMISSIONS

            if is_admin(user) and not role_has_full_access(role):
                return error_response("You cannot remove your own admin access", 400)
            current_keys = {perm.key for perm in user.role.permissions} if user.role else set()
            if set(ROLE_MGMT_PERMISSIONS) & current_keys:
                new_keys = {perm.key for perm in role.permissions}
                if not (set(ROLE_MGMT_PERMISSIONS) & new_keys):
                    return error_response("You cannot remove your own permission to manage roles", 400)
            if set(USER_MGMT_PERMISSIONS) & current_keys:
                new_keys = {perm.key for perm in role.permissions}
                if not (set(USER_MGMT_PERMISSIONS) & new_keys):
                    return error_response("You cannot remove your own permission to manage users", 400)
        user.role_id = role.id
    if "isActive" in payload:
        new_active = bool(payload.get("isActive"))
        if actor and actor.id == user.id and not new_active:
            return error_response("You cannot disable your own account", 400)
        if user.is_active and not new_active and is_admin(user) and user_service.active_admin_count() <= 1:
            return error_response("Cannot disable the last active admin", 400)
        user.is_active = new_active
    if payload.get("password"):
        user.password_hash = hash_password(payload["password"])

    if "accessRules" in payload:
        apply_user_access(user, [], [], access_rules=payload.get("accessRules") or [])
    elif "clusterAccess" in payload or "namespaceAccess" in payload:
        cluster_ids = parse_cluster_access_payload(payload.get("clusterAccess"))
        namespace_rows = parse_namespace_access_payload(payload.get("namespaceAccess"))
        apply_user_access(user, cluster_ids, namespace_rows)

    db.session.commit()
    log_audit(
        "user_updated",
        actor=get_current_user(),
        target_type="user",
        target_id=str(user.id),
        details={"username": user.username},
    )
    if "roleId" in payload and user.role_id != previous_role_id:
        log_audit(
            "user_role_changed",
            actor=get_current_user(),
            target_type="user",
            target_id=str(user.id),
            details={
                "username": user.username,
                "previousRoleId": previous_role_id,
                "previousRole": previous_role_name,
                "newRoleId": user.role_id,
                "newRole": user.role.name if user.role else None,
            },
        )
    return success_response(user_to_dict(user, include_access=True))


@users_bp.route("/<int:user_id>", methods=["DELETE"])
@require_permission("users:disable")
def disable_user(user_id: int):
    actor = get_current_user()
    if actor and actor.id == user_id:
        return error_response("You cannot disable your own account", 400)
    data, error, status = user_service.disable_user(user_id)
    if error:
        return error_response(error, status)
    return success_response({"id": data["id"], "isActive": data.get("isActive", False)})


@users_bp.route("/<int:user_id>/permanent", methods=["DELETE"])
@require_any_permission("users:delete", "users:manage")
def delete_user(user_id: int):
    actor = get_current_user()
    if actor and actor.id == user_id:
        return error_response("You cannot delete your own account", 400)
    data, error, status = user_service.delete_user(user_id)
    if error:
        return error_response(error, status)
    return success_response(data)
