"""Pipelines outside CI services, and a service's links to the inventory.

Kept beside ``routes/ci.py`` like the schedules and merge check routes: the
same ``/api/ci`` prefix and service lookup, nothing else shared.

A pipeline on the Pipelines page is stored as a service row of kind
``pipeline`` (services/ci/shared_pipelines.py), so everything a pipeline does
once it exists — edit stages, run, builds, logs, secrets, schedules, repository,
rename, delete — goes through the ordinary ``/api/ci/services/<id>/…`` and
``/api/ci/pipelines/<id>`` routes. Only what is new lives here: listing and
creating them, who uses one, attaching one to a service, and deployment links.

Permissions are the existing CI keys: viewing needs ``ci_pipelines:view``;
creating one needs ``ci_services:create`` and ``ci_pipelines:edit`` (it is a
new buildable thing AND a pipeline definition); attaching one changes how a
service builds, so ``ci_pipelines:edit``; links describe the service, so
``ci_services:view`` / ``ci_services:edit``.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.ci import catalog as catalog_service
from ..services.ci import deploy_templates as templates_service
from ..services.ci import deployment_links as links_service
from ..services.ci import pipelines as pipelines_service
from ..services.ci import shared_pipelines as shared_service

ci_shared_bp = Blueprint("ci_shared_pipelines", __name__, url_prefix="/api/ci")

_USER_ERRORS = (
    shared_service.SharedPipelineError,
    links_service.DeploymentLinkError,
    pipelines_service.PipelineError,
    catalog_service.CatalogError,
)


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@ci_shared_bp.errorhandler(LookupError)
def _not_found(exc: LookupError):
    return error_response(str(exc) or "Not found.", 404)


def _catalog_service(service_id: int):
    row = catalog_service.get_service(service_id)
    if row.is_pipeline_home:
        raise LookupError("Service not found.")
    return row


# ---------------------------------------------------------------------------
# Pipelines page
# ---------------------------------------------------------------------------

@ci_shared_bp.route("/shared-pipelines", methods=["GET"])
@require_permission("ci_pipelines:view")
def list_shared_pipelines():
    items = shared_service.list_homes(search=request.args.get("search", ""))
    return success_response(
        {"items": items, "count": len(items), "summary": shared_service.list_summary(items)}
    )


@ci_shared_bp.route("/shared-pipelines", methods=["POST"])
@require_permission("ci_services:create")
@require_permission("ci_pipelines:edit")
def create_shared_pipeline():
    try:
        data = shared_service.create_home(_payload(), actor=get_current_user())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_shared_bp.route("/shared-pipelines/<int:home_id>", methods=["GET"])
@require_permission("ci_pipelines:view")
def get_shared_pipeline(home_id: int):
    home = shared_service.get_home(home_id)
    return success_response(shared_service.home_to_dict(home, include_used_by=True))


@ci_shared_bp.route("/shared-pipelines/<int:home_id>/copy-from/<int:service_id>", methods=["GET"])
@require_permission("ci_pipelines:view")
def copy_from_service(home_id: int, service_id: int):
    """A service's pipeline as a draft for this one's editor. Writes nothing:
    the editor shows it as unsaved changes, and a save validates it as usual."""
    shared_service.get_home(home_id)
    service = _catalog_service(service_id)
    try:
        return success_response(shared_service.copy_payload_of(service))
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)


# ---------------------------------------------------------------------------
# A service using one
# ---------------------------------------------------------------------------

@ci_shared_bp.route("/services/<int:service_id>/shared-pipeline", methods=["POST"])
@require_permission("ci_pipelines:edit")
def attach_shared_pipeline(service_id: int):
    service = _catalog_service(service_id)
    payload = _payload()
    try:
        home = shared_service.get_home(int(payload.get("sharedPipelineId")))
    except (TypeError, ValueError):
        return error_response("Send sharedPipelineId: the pipeline to use.", 400)
    try:
        data = shared_service.attach(service, home, actor=get_current_user())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_shared_bp.route("/services/<int:service_id>/shared-pipeline/detach", methods=["POST"])
@require_permission("ci_pipelines:edit")
def detach_shared_pipeline(service_id: int):
    service = _catalog_service(service_id)
    try:
        data = shared_service.detach(
            service, mode=str(_payload().get("mode") or "restore"), actor=get_current_user()
        )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


# ---------------------------------------------------------------------------
# Inventory templates a Deploy stage or a link can create from
# ---------------------------------------------------------------------------

@ci_shared_bp.route("/deploy-templates", methods=["GET"])
@require_permission("ci_pipelines:view")
def list_deploy_templates():
    """The inventory's deployment templates, as a Deploy stage picker needs them.

    Summaries only (name, category, containers, default image), so a person who
    may edit pipelines can pick one without being able to manage templates.
    """
    items = templates_service.summaries()
    return success_response({"items": items, "count": len(items)})


@ci_shared_bp.route("/deploy-templates/<template_id>/preview", methods=["POST"])
@require_permission("ci_pipelines:view")
def preview_deploy_template(template_id: str):
    """What a build would create from this template in that namespace, or why
    it cannot. Never applies anything."""
    payload = _payload()
    try:
        text, created = templates_service.render(
            template_id,
            namespace=str(payload.get("namespace") or "default"),
            deployment_name=str(payload.get("deploymentName") or ""),
            container_name=str(payload.get("containerName") or ""),
            answers=_preview_answers(payload.get("answers")),
        )
    except templates_service.DeployTemplateError as exc:
        return success_response({"ok": False, "error": str(exc)})
    return success_response({"ok": True, "yaml": text, "creates": created})


def _preview_answers(value):
    from ..services.ci import deploy_config

    try:
        return deploy_config.template_answers(value, "The template")
    except deploy_config.DeployConfigError as exc:
        raise templates_service.DeployTemplateError(str(exc))


# ---------------------------------------------------------------------------
# Deployment links
# ---------------------------------------------------------------------------

@ci_shared_bp.route("/services/<int:service_id>/deployments", methods=["GET"])
@require_permission("ci_services:view")
def list_deployment_links(service_id: int):
    service = _catalog_service(service_id)
    live = request.args.get("live", "true").lower() not in ("0", "false", "no")
    items = links_service.list_links(service, user=get_current_user(), live=live)
    return success_response({"items": items, "count": len(items)})


@ci_shared_bp.route("/services/<int:service_id>/deployments", methods=["POST"])
@require_permission("ci_services:edit")
def add_deployment_link(service_id: int):
    service = _catalog_service(service_id)
    payload = _payload()
    source = "inventory" if payload.get("source") == "inventory" else "manual"
    try:
        data = links_service.add_link(service, payload, actor=get_current_user(), source=source)
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_shared_bp.route("/services/<int:service_id>/deployments/<int:link_id>", methods=["PUT"])
@require_permission("ci_services:edit")
def update_deployment_link(service_id: int, link_id: int):
    service = _catalog_service(service_id)
    row = links_service.get_link(service, link_id)
    try:
        data = links_service.update_link(row, _payload(), actor=get_current_user())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_shared_bp.route("/services/<int:service_id>/deployments/<int:link_id>", methods=["DELETE"])
@require_permission("ci_services:edit")
def remove_deployment_link(service_id: int, link_id: int):
    service = _catalog_service(service_id)
    row = links_service.get_link(service, link_id)
    links_service.remove_link(row, actor=get_current_user())
    return success_response({"deleted": True})
