"""Smart Deployment Form API — generate a fillable Excel form from a template,
import the filled file, validate it, and hand off to the wizard / bundle / approval.

Nothing here deploys. Import only parses + validates + prefills; deployment happens
only through the wizard's existing apply flow or an approved change bundle.
"""

from __future__ import annotations

from flask import Blueprint, Response, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.deployment_form_service import (
    add_import_to_bundle,
    build_wizard_state,
    generate_form,
    get_import,
    import_form,
    revalidate_import,
    send_import_for_approval,
)

deployment_forms_bp = Blueprint("deployment_forms", __name__, url_prefix="/api/deployment-forms")

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _body() -> dict:
    return request.get_json(silent=True) or {}


@deployment_forms_bp.route("/generate", methods=["POST"])
@require_permission("apps:dryrun")
def generate():
    body = _body()
    template_id = str(body.get("templateId") or body.get("template_id") or "").strip()
    if not template_id:
        return error_response("templateId is required.", 400)
    cluster_id = (body.get("clusterId") or body.get("cluster_id") or "").strip() or None
    namespace = (body.get("namespace") or "").strip() or None

    data, error, status = generate_form(
        get_current_user(), template_id, cluster_id=cluster_id, namespace=namespace
    )
    if error:
        return error_response(error, status)

    response = Response(data["bytes"], mimetype=_XLSX_MIME)
    response.headers["Content-Disposition"] = f'attachment; filename="{data["filename"]}"'
    response.headers["X-Deployment-Form-Id"] = data["formUid"]
    return response


@deployment_forms_bp.route("/import", methods=["POST"])
@require_permission("apps:dryrun")
def import_deployment_form():
    upload = request.files.get("file")
    if upload is None:
        return error_response("Attach the filled deployment form (.xlsx).", 400)
    file_bytes = upload.read()
    if not file_bytes:
        return error_response("The uploaded file is empty.", 400)

    data, error, status = import_form(get_current_user(), file_bytes)
    if error:
        return error_response(error, status)
    return success_response(data)


@deployment_forms_bp.route("/imports/<int:import_id>", methods=["GET"])
@require_permission("apps:dryrun")
def get_form_import(import_id: int):
    data, error, status = get_import(get_current_user(), import_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@deployment_forms_bp.route("/imports/<int:import_id>/validate", methods=["POST"])
@require_permission("apps:dryrun")
def validate_form_import(import_id: int):
    data, error, status = revalidate_import(get_current_user(), import_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@deployment_forms_bp.route("/imports/<int:import_id>/apply-to-wizard", methods=["POST"])
@require_permission("apps:dryrun")
def apply_import_to_wizard(import_id: int):
    data, error, status = build_wizard_state(get_current_user(), import_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@deployment_forms_bp.route("/imports/<int:import_id>/add-to-bundle", methods=["POST"])
@require_permission("change_bundles:create")
def add_form_import_to_bundle(import_id: int):
    data, error, status = add_import_to_bundle(get_current_user(), import_id)
    if error:
        return error_response(error, status)
    return success_response(data)


@deployment_forms_bp.route("/imports/<int:import_id>/send-for-approval", methods=["POST"])
@require_permission("change_bundles:create")
def send_form_import_for_approval(import_id: int):
    body = _body()
    data, error, status = send_import_for_approval(
        get_current_user(),
        import_id,
        note=str(body.get("note") or ""),
        window_start=body.get("windowStart") or body.get("window_start"),
        window_end=body.get("windowEnd") or body.get("window_end"),
        window_timezone=body.get("windowTimezone") or body.get("window_timezone"),
        stop_on_failure=body.get("stopOnFailure", body.get("stop_on_failure", True)),
    )
    if error:
        return error_response(error, status)
    return success_response(data)
