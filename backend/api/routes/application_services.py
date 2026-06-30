from flask import Blueprint, g, request

from ..auth_utils import get_current_user
from ..decorators import require_any_permission, require_permission
from ..k8s_provider import should_use_real_k8s
from ..response import error_response, success_response
from ..services.application_service_service import (
    create_service,
    delete_service,
    get_service,
    get_service_mock,
    list_picker_deployments,
    list_picker_pods,
    list_picker_workloads,
    list_services,
    list_services_mock,
    update_service,
)

app_services_bp = Blueprint("app_services", __name__, url_prefix="/api/application-services")


def _actor_user_id() -> int | None:
    user = getattr(g, "current_user", None)
    return user.id if user else None


def _use_mock() -> bool:
    return not should_use_real_k8s()


# ---------------------------------------------------------------------------
# List & Create
# ---------------------------------------------------------------------------

@app_services_bp.route("", methods=["GET"])
@require_permission("app_services:view")
def list_app_services():
    user = get_current_user()
    # Prefer real, DB-backed services (incl. those created by Deploy From
    # Blueprint). Only fall back to the demo/mock list when there are none and
    # no live cluster is configured, so a fresh install still shows the demo.
    real = list_services(user=user)
    if real.get("count", 0) == 0 and _use_mock():
        return success_response(list_services_mock())
    return success_response(real)


@app_services_bp.route("", methods=["POST"])
@require_permission("app_services:create")
def create_app_service():
    payload = request.get_json(silent=True) or {}
    data, error, status = create_service(payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


# ---------------------------------------------------------------------------
# Single resource
# ---------------------------------------------------------------------------

@app_services_bp.route("/<int:service_id>", methods=["GET"])
@require_permission("app_services:view")
def get_app_service(service_id: int):
    user = get_current_user()
    # Prefer a real DB service; fall back to the demo/mock service only when not
    # found and no live cluster is configured.
    data, error, status = get_service(service_id, user=user)
    if error and status == 404 and _use_mock():
        data, error, status = get_service_mock(service_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@app_services_bp.route("/<int:service_id>", methods=["PUT"])
@require_permission("app_services:update")
def update_app_service(service_id: int):
    payload = request.get_json(silent=True) or {}
    data, error, status = update_service(service_id, payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


@app_services_bp.route("/<int:service_id>", methods=["DELETE"])
@require_permission("app_services:delete")
def delete_app_service(service_id: int):
    data, error, status = delete_service(service_id, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


# ---------------------------------------------------------------------------
# Deployment picker — returns deployment names the requesting user can see
# ---------------------------------------------------------------------------

@app_services_bp.route("/picker/deployments", methods=["GET"])
@require_any_permission("app_services:create", "app_services:update")
def picker_deployments():
    cluster_id = (request.args.get("clusterId") or "").strip()
    namespace = (request.args.get("namespace") or "").strip()
    user = get_current_user()
    data, error, status = list_picker_deployments(cluster_id, namespace, user=user)
    if error:
        return error_response(error, status)
    return success_response(data)


@app_services_bp.route("/picker/pods", methods=["GET"])
@require_any_permission("app_services:create", "app_services:update")
def picker_pods():
    cluster_id = (request.args.get("clusterId") or "").strip()
    namespace = (request.args.get("namespace") or "").strip()
    user = get_current_user()
    data, error, status = list_picker_pods(cluster_id, namespace, user=user)
    if error:
        return error_response(error, status)
    return success_response(data)


@app_services_bp.route("/picker/workloads", methods=["GET"])
@require_any_permission("app_services:create", "app_services:update")
def picker_workloads():
    cluster_id = (request.args.get("clusterId") or "").strip()
    namespace = (request.args.get("namespace") or "").strip()
    kind = (request.args.get("kind") or "deployment").strip()
    user = get_current_user()
    data, error, status = list_picker_workloads(cluster_id, namespace, kind, user=user)
    if error:
        return error_response(error, status)
    return success_response(data)
