from flask import Blueprint, g, request

from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.blueprint_deploy_service import (
    build_deploy_plan,
    delete_app_service,
    deploy_from_blueprint,
    get_app_service,
    list_app_services,
)
from ..services.blueprint_picker_service import (
    list_cluster_resources,
    list_namespaced_resources,
    list_namespaces,
)
from ..services.service_blueprint_service import (
    create_blueprint,
    delete_blueprint,
    get_blueprint,
    list_blueprints,
    update_blueprint,
)

service_blueprints_bp = Blueprint("service_blueprints", __name__, url_prefix="/api/service-blueprints")
blueprint_app_services_bp = Blueprint("blueprint_app_services", __name__, url_prefix="/api/app-services")


def _actor_user_id():
    user = getattr(g, "current_user", None)
    return user.id if user else None


# ---------------------------------------------------------------------------
# Service blueprints — CRUD
# ---------------------------------------------------------------------------

@service_blueprints_bp.route("", methods=["GET"])
@require_permission("service_blueprints:view")
def list_all_blueprints():
    return success_response(list_blueprints())


@service_blueprints_bp.route("/<int:blueprint_id>", methods=["GET"])
@require_permission("service_blueprints:view")
def get_single_blueprint(blueprint_id: int):
    data, error, status = get_blueprint(blueprint_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@service_blueprints_bp.route("", methods=["POST"])
@require_permission("service_blueprints:create")
def create_new_blueprint():
    payload = request.get_json(silent=True) or {}
    data, error, status = create_blueprint(payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@service_blueprints_bp.route("/<int:blueprint_id>", methods=["PUT"])
@require_permission("service_blueprints:update")
def update_existing_blueprint(blueprint_id: int):
    payload = request.get_json(silent=True) or {}
    data, error, status = update_blueprint(blueprint_id, payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


@service_blueprints_bp.route("/<int:blueprint_id>", methods=["DELETE"])
@require_permission("service_blueprints:delete")
def delete_existing_blueprint(blueprint_id: int):
    data, error, status = delete_blueprint(blueprint_id, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)


# ---------------------------------------------------------------------------
# Deploy From Blueprint
# ---------------------------------------------------------------------------

@service_blueprints_bp.route("/<int:blueprint_id>/deploy-plan", methods=["POST"])
@require_permission("service_blueprints:deploy")
def blueprint_deploy_plan(blueprint_id: int):
    target = request.get_json(silent=True) or {}
    user = getattr(g, "current_user", None)
    data, error, status = build_deploy_plan(blueprint_id, target, user=user)
    if error:
        return error_response(error, status)
    return success_response(data)


@service_blueprints_bp.route("/<int:blueprint_id>/deploy", methods=["POST"])
@require_permission("service_blueprints:deploy")
def blueprint_deploy(blueprint_id: int):
    payload = request.get_json(silent=True) or {}
    data, error, status = deploy_from_blueprint(blueprint_id, payload, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data, status_code=status)


@service_blueprints_bp.route("/<int:blueprint_id>/app-services", methods=["GET"])
@require_permission("service_blueprints:view")
def blueprint_app_services(blueprint_id: int):
    return success_response(list_app_services(blueprint_id=blueprint_id))


# ---------------------------------------------------------------------------
# Live resource pickers for the deploy wizard
# ---------------------------------------------------------------------------

@service_blueprints_bp.route("/pickers/namespaces", methods=["GET"])
@require_permission("service_blueprints:deploy")
def picker_namespaces():
    user = getattr(g, "current_user", None)
    data, error, status = list_namespaces(request.args.get("clusterId", ""), user=user)
    if error:
        return error_response(error, status)
    return success_response(data)


@service_blueprints_bp.route("/pickers/resources", methods=["GET"])
@require_permission("service_blueprints:deploy")
def picker_namespaced_resources():
    user = getattr(g, "current_user", None)
    data, error, status = list_namespaced_resources(
        request.args.get("clusterId", ""),
        request.args.get("namespace", ""),
        request.args.get("kind", ""),
        user=user,
        secret_type=request.args.get("secretType"),
    )
    if error:
        return error_response(error, status)
    return success_response(data)


@service_blueprints_bp.route("/pickers/cluster-resources", methods=["GET"])
@require_permission("service_blueprints:deploy")
def picker_cluster_resources():
    user = getattr(g, "current_user", None)
    data, error, status = list_cluster_resources(
        request.args.get("clusterId", ""),
        request.args.get("kind", ""),
        user=user,
    )
    if error:
        return error_response(error, status)
    return success_response(data)


# ---------------------------------------------------------------------------
# App services (blueprint instances)
# ---------------------------------------------------------------------------

@blueprint_app_services_bp.route("", methods=["GET"])
@require_permission("app_services:view")
def list_all_app_services():
    client_id = request.args.get("clientId")
    blueprint_id = request.args.get("blueprintId")
    return success_response(
        list_app_services(
            client_id=int(client_id) if client_id and client_id.isdigit() else None,
            blueprint_id=int(blueprint_id) if blueprint_id and blueprint_id.isdigit() else None,
        )
    )


@blueprint_app_services_bp.route("/<int:app_service_id>", methods=["GET"])
@require_permission("app_services:view")
def get_single_app_service(app_service_id: int):
    user = getattr(g, "current_user", None)
    data, error, status = get_app_service(app_service_id, user=user)
    if error:
        return error_response(error, status)
    return success_response(data)


@blueprint_app_services_bp.route("/<int:app_service_id>", methods=["DELETE"])
@require_permission("app_services:delete")
def delete_single_app_service(app_service_id: int):
    data, error, status = delete_app_service(app_service_id, actor_user_id=_actor_user_id())
    if error:
        return error_response(error, status)
    return success_response(data)
