from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.alert_policy_evaluator import list_alert_history, list_active_policy_alerts
from ..services.alert_policy_service import (
    create_policy,
    delete_policy,
    get_catalog,
    get_policy,
    list_policies,
    policy_stats,
    set_policy_enabled,
    update_policy,
)

alert_policies_bp = Blueprint("alert_policies", __name__, url_prefix="/api/alert-policies")


@alert_policies_bp.route("/catalog", methods=["GET"])
@require_permission("alerts:view")
def alert_policy_catalog():
    return success_response(get_catalog())


@alert_policies_bp.route("", methods=["GET"])
@require_permission("alerts:view")
def alert_policies_list():
    user = get_current_user()
    cluster_id = request.args.get("cluster", "").strip() or None
    items = list_policies(user, cluster_id=cluster_id)
    return success_response({"items": items, "count": len(items)})


@alert_policies_bp.route("", methods=["POST"])
@require_permission("alerts:manage")
def alert_policies_create():
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    data, error, status = create_policy(user, payload)
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@alert_policies_bp.route("/<int:policy_id>", methods=["GET"])
@require_permission("alerts:view")
def alert_policies_get(policy_id: int):
    user = get_current_user()
    data, error, status = get_policy(user, policy_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@alert_policies_bp.route("/<int:policy_id>", methods=["PUT"])
@require_permission("alerts:manage")
def alert_policies_update(policy_id: int):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    data, error, status = update_policy(user, policy_id, payload)
    if error:
        return error_response(error, status)
    return success_response(data)


@alert_policies_bp.route("/<int:policy_id>", methods=["DELETE"])
@require_permission("alerts:manage")
def alert_policies_delete(policy_id: int):
    user = get_current_user()
    data, error, status = delete_policy(user, policy_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@alert_policies_bp.route("/<int:policy_id>/status", methods=["PATCH"])
@require_permission("alerts:manage")
def alert_policies_status(policy_id: int):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload:
        return error_response("enabled field is required", 400)
    data, error, status = set_policy_enabled(user, policy_id, bool(payload.get("enabled")))
    if error:
        return error_response(error, status)
    return success_response(data)


@alert_policies_bp.route("/stats", methods=["GET"])
@require_permission("alerts:view")
def alert_policies_stats():
    cluster_id = request.args.get("cluster", "").strip() or None
    return success_response(policy_stats(cluster_id=cluster_id))


@alert_policies_bp.route("/history", methods=["GET"])
@require_permission("alerts:view")
def alert_policies_history():
    user = get_current_user()
    cluster_id = request.args.get("cluster", "").strip() or None
    status = request.args.get("status", "").strip() or None
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        limit = 100
    items = list_alert_history(cluster_id=cluster_id, status=status, limit=limit, user=user)
    return success_response({"items": items, "count": len(items)})


@alert_policies_bp.route("/evaluate", methods=["POST"])
@require_permission("alerts:manage")
def alert_policies_evaluate():
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    cluster_id = str(payload.get("clusterId") or request.args.get("cluster", "")).strip()
    if not cluster_id:
        return error_response("clusterId is required", 400)
    items = list_active_policy_alerts(cluster_id=cluster_id, user=user)
    return success_response({"items": items, "count": len(items)})
