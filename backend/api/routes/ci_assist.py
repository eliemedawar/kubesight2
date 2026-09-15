"""Assisted CI configuration API.

A separate blueprint from ``routes/ci.py``, mounted under the same ``/api/ci``
prefix so the URLs read as one API. The separation is the same one the service
layer makes: CI proper must not import the model path, and a route module that
did would drag it in.

Permissions are the existing keys, deliberately doubled up on the analyse
route: using the model path needs BOTH the right to edit a pipeline and the
right to ask Hermes for an analysis. Holding CI permissions alone should not
silently grant the ability to send an organisation's source to a model.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.ci import catalog as catalog_service
from ..services.ci import generated as generated_service
from ..services.ci_assist import accept as accept_service
from ..services.ci_assist import analyses as analyses_service
from ..services.ci_assist import profile as profile_service

ci_assist_bp = Blueprint("ci_assist", __name__, url_prefix="/api/ci")

_USER_ERRORS = (
    analyses_service.AnalysisError,
    accept_service.AcceptError,
    profile_service.ProfileError,
    catalog_service.CatalogError,
)


def _actor():
    return get_current_user()


def _payload() -> dict:
    return request.get_json(silent=True) or {}


@ci_assist_bp.errorhandler(LookupError)
def _not_found(exc: LookupError):
    return error_response(str(exc) or "Not found.", 404)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

@ci_assist_bp.route("/assist/availability", methods=["GET"])
@require_permission("ci_services:view")
def assist_availability():
    """Whether the assisted path may be offered, before it is offered.

    The registration wizard asks this to decide whether to show the choice at
    all — an installation with no Hermes shows manual configuration and the
    reason, never a control that fails when pressed.
    """
    return success_response(analyses_service.availability())


@ci_assist_bp.route("/assist/capabilities", methods=["GET"])
@require_permission("ci_pipelines:view")
def assist_capabilities():
    """What a generated pipeline is allowed to use.

    The same menu sent to Hermes and enforced by the validator. Exposed because
    the manual path benefits from it too: the build environment catalog is what
    a person picking a stage image should be choosing from.
    """
    from ..services.ci_assist import generator

    return success_response(generator.capabilities_payload())


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@ci_assist_bp.route("/services/<int:service_id>/analysis", methods=["POST"])
@require_permission("ci_pipelines:edit")
@require_permission("applications:analyze")
def start_analysis(service_id: int):
    """Analyze the repository and propose a pipeline.

    Returns immediately with the analysis row; the work happens on a worker and
    the client polls the GET. 202 rather than 201 because what comes back is a
    job in progress, not a finished thing.
    """
    service = catalog_service.get_service(service_id)
    try:
        data = analyses_service.request_analysis(service, _payload(), actor=_actor())
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=202)


@ci_assist_bp.route("/services/<int:service_id>/analysis", methods=["GET"])
@require_permission("ci_services:view")
def get_latest_analysis(service_id: int):
    """The service's assisted-configuration state — the poll target."""
    service = catalog_service.get_service(service_id)
    return success_response(analyses_service.service_state(service))


@ci_assist_bp.route("/services/<int:service_id>/analyses", methods=["GET"])
@require_permission("ci_services:view")
def list_analyses(service_id: int):
    catalog_service.get_service(service_id)
    rows = analyses_service.list_for_service(service_id)
    items = [analyses_service.analysis_to_dict(row) for row in rows]
    return success_response({"items": items, "count": len(items)})


@ci_assist_bp.route("/analyses/<int:analysis_id>", methods=["GET"])
@require_permission("ci_services:view")
def get_analysis(analysis_id: int):
    row = analyses_service.get_analysis(analysis_id)
    return success_response(analyses_service.analysis_to_dict(row))


@ci_assist_bp.route("/analyses/<int:analysis_id>/cancel", methods=["POST"])
@require_permission("ci_pipelines:edit")
def cancel_analysis(analysis_id: int):
    row = analyses_service.get_analysis(analysis_id)
    try:
        return success_response(analyses_service.cancel(row, actor=_actor()))
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)


@ci_assist_bp.route("/analyses/<int:analysis_id>/accept", methods=["POST"])
@require_permission("ci_pipelines:edit")
def accept_analysis(analysis_id: int):
    """Save an approved proposal as a normal KubeSight pipeline.

    After this the service builds through the ordinary engine and never
    consults Hermes again. Secret VALUES may arrive in this payload, which is
    why it also needs the right to manage secrets when it carries any.
    """
    row = analyses_service.get_analysis(analysis_id)
    payload = _payload()
    actor = _actor()

    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    declares_secret = any(
        str(item.get("kind")) == "secret" and inputs.get(str(item.get("name")))
        for item in (row.required_inputs or [])
        if isinstance(item, dict)
    )
    if declares_secret and not _has_permission(actor, "ci_secrets:manage"):
        return error_response(
            "Saving this proposal stores secret values, which needs the "
            "'Manage CI secrets' permission.",
            403,
        )

    try:
        result = accept_service.accept(row, payload, actor=actor)
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(
        {
            "pipeline": result["pipeline"],
            "service": catalog_service.service_detail(result["service"]),
        }
    )


def _has_permission(user, key: str) -> bool:
    """Whether this user holds one permission, without a second auth path.

    Route-level gating is still ``@require_permission``; this is the one place
    a SECOND key depends on what the payload contains, and it asks the same
    access engine that decorator does.
    """
    from ..access_engine import user_has_permission

    try:
        return bool(user_has_permission(user, key))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Profile and validation
# ---------------------------------------------------------------------------

@ci_assist_bp.route("/services/<int:service_id>/profile", methods=["PUT"])
@require_permission("ci_services:edit")
def update_profile(service_id: int):
    """Correct what KubeSight believes this application is.

    A user-set field stays user-set: it is recorded as an override so a later
    regenerate builds around it instead of quietly detecting over the top of it.
    """
    service = catalog_service.get_service(service_id)
    payload = _payload()
    try:
        if payload.get("overrides"):
            resolved = profile_service.apply_overrides(
                service.application_profile or {}, payload["overrides"], actor=_actor()
            )
        else:
            resolved = profile_service.normalize(
                payload.get("applicationProfile") or {}, source="manual"
            )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)

    try:
        data = catalog_service.update_application_profile(
            service, resolved, actor=_actor()
        )
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_assist_bp.route("/pipelines/validate-generated", methods=["POST"])
@require_permission("ci_pipelines:view")
def validate_generated():
    """Run a proposed pipeline past KubeSight's authority without saving it.

    The review screen calls this after every edit, so a person correcting a
    proposal is told the same things Hermes would have been told, in the same
    words, before they press Create.
    """
    payload = _payload()
    service = None
    if payload.get("serviceId"):
        try:
            service = catalog_service.get_service(int(payload["serviceId"]))
        except (TypeError, ValueError):
            return error_response("serviceId must be a service id.", 400)
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, dict):
        return error_response("Send pipeline: {stages: [...]} to validate.", 400)
    verdict = generated_service.validate(
        service, pipeline, declared_inputs=payload.get("requiredInputs") or []
    )
    return success_response(verdict)
