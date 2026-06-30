from flask import Blueprint, g, request

from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.topology_component_service import (
    create_component,
    delete_component,
    get_component,
    list_components,
    record_heartbeat,
    run_health_check,
    update_component,
)

topology_components_bp = Blueprint("topology_components", __name__, url_prefix="/api/topology-components")


def _actor_user_id() -> int | None:
    user = getattr(g, "current_user", None)
    return user.id if user else None


@topology_components_bp.route("", methods=["GET"])
@require_permission("components:view")
def list_topology_components():
    return success_response(list_components())


@topology_components_bp.route("", methods=["POST"])
@require_permission("components:create")
def create_topology_component():
    payload = request.get_json(silent=True) or {}
    data, error, status = create_component(payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@topology_components_bp.route("/<int:component_id>", methods=["GET"])
@require_permission("components:view")
def get_topology_component(component_id: int):
    data, error, status = get_component(component_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@topology_components_bp.route("/<int:component_id>", methods=["PUT"])
@require_permission("components:update")
def update_topology_component(component_id: int):
    payload = request.get_json(silent=True) or {}
    data, error, status = update_component(component_id, payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


@topology_components_bp.route("/<int:component_id>", methods=["DELETE"])
@require_permission("components:delete")
def delete_topology_component(component_id: int):
    data, error, status = delete_component(component_id, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


@topology_components_bp.route("/<int:component_id>/check", methods=["POST"])
@require_permission("components:check")
def check_topology_component(component_id: int):
    data, error, status = run_health_check(component_id, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


# Inbound webhook heartbeat. Intentionally NOT behind @require_permission — it is
# called by external monitors and is gated by the per-component webhook token.
@topology_components_bp.route("/<int:component_id>/heartbeat", methods=["POST"])
def topology_component_heartbeat(component_id: int):
    payload = request.get_json(silent=True) or {}
    token = (
        request.args.get("token")
        or request.headers.get("X-Webhook-Token")
        or payload.get("token")
        or ""
    )
    data, error, status = record_heartbeat(component_id, token, status=payload.get("status"))
    if error:
        return error_response(error, status)
    return success_response(data)
