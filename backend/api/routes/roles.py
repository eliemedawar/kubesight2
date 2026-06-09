from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_any_permission, require_permission
from ..response import error_response, success_response
from ..services import role_service

roles_bp = Blueprint("roles", __name__, url_prefix="/api")


@roles_bp.route("/roles", methods=["GET"])
@require_permission("roles:view")
def list_roles():
    return success_response(role_service.list_roles())


@roles_bp.route("/roles", methods=["POST"])
@require_any_permission("roles:manage", "users:manage")
def create_role():
    payload = request.get_json(silent=True) or {}
    data, error, status = role_service.create_role(payload, actor=get_current_user())
    if error:
        return error_response(error, status)
    return success_response(data, status)


@roles_bp.route("/roles/<int:role_id>", methods=["GET"])
@require_permission("roles:view")
def get_role(role_id: int):
    data, error, status = role_service.get_role(role_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@roles_bp.route("/roles/<int:role_id>", methods=["PUT"])
@require_any_permission("roles:manage", "users:manage")
def update_role(role_id: int):
    payload = request.get_json(silent=True) or {}
    data, error, status = role_service.update_role(role_id, payload, actor=get_current_user())
    if error:
        return error_response(error, status)
    return success_response(data)


@roles_bp.route("/roles/<int:role_id>", methods=["DELETE"])
@require_any_permission("roles:manage", "users:manage")
def delete_role(role_id: int):
    data, error, status = role_service.delete_role(role_id, actor=get_current_user())
    if error:
        return error_response(error, status)
    return success_response(data)


@roles_bp.route("/permissions", methods=["GET"])
@require_permission("roles:view")
def list_permissions():
    return success_response(role_service.list_permissions())


@roles_bp.route("/roles/<int:role_id>/permissions", methods=["PUT"])
@require_any_permission("roles:manage", "users:manage")
def update_role_permissions(role_id: int):
    payload = request.get_json(silent=True) or {}
    keys = payload.get("permissions") or []
    data, error, status = role_service.update_role_permissions(
        role_id,
        keys,
        actor=get_current_user(),
    )
    if error:
        return error_response(error, status)
    return success_response(data)
