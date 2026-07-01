"""Linked image registry connections (e.g. Sonatype Nexus).

CRUD + a connectivity test + an on-demand image-availability check the Deploy
Wizard uses to show a live ✅/❌ as the user types an image reference.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import registry_service as svc

registries_bp = Blueprint("registries", __name__, url_prefix="/api/registries")


@registries_bp.route("", methods=["GET"])
@require_permission("registries:view")
def list_registries():
    return success_response({"items": svc.list_connections()})


@registries_bp.route("", methods=["POST"])
@require_permission("registries:manage")
def create_registry():
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.create_connection(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "registry_connection_created",
        actor=get_current_user(),
        target_type="registry_connection",
        target_id=str(data.get("id")),
        details={"name": data.get("name"), "baseUrl": data.get("baseUrl")},
    )
    return success_response(data, status_code=201)


@registries_bp.route("/<int:connection_id>", methods=["PUT"])
@require_permission("registries:manage")
def update_registry(connection_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.update_connection(connection_id, payload)
    except LookupError:
        return error_response("Registry connection not found.", 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "registry_connection_updated",
        actor=get_current_user(),
        target_type="registry_connection",
        target_id=str(connection_id),
        details={"name": data.get("name"), "baseUrl": data.get("baseUrl")},
    )
    return success_response(data)


@registries_bp.route("/<int:connection_id>", methods=["DELETE"])
@require_permission("registries:manage")
def delete_registry(connection_id: int):
    try:
        svc.delete_connection(connection_id)
    except LookupError:
        return error_response("Registry connection not found.", 404)
    log_audit(
        "registry_connection_deleted",
        actor=get_current_user(),
        target_type="registry_connection",
        target_id=str(connection_id),
    )
    return success_response({"deleted": True})


@registries_bp.route("/<int:connection_id>/test", methods=["POST"])
@require_permission("registries:manage")
def test_registry(connection_id: int):
    try:
        return success_response(svc.test_connection(connection_id))
    except LookupError:
        return error_response("Registry connection not found.", 404)


@registries_bp.route("/check-image", methods=["POST"])
@require_permission("registries:view")
def check_image():
    payload = request.get_json(silent=True) or {}
    image = str(payload.get("image") or "").strip()
    if not image:
        return error_response("An image reference is required.", 400)
    return success_response(svc.check_image(image))
