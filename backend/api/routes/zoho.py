"""Zoho Desk DevOps Request field-sync integration.

Config CRUD + a connection test + a manual sync trigger + a preview of what would
be published, all permission-gated. Plus the inbound webhook (``/inbound``) that
Zoho calls when a DevOps Request ticket is created — it is NOT session
authenticated (Zoho has no KubeSight login); instead it verifies a shared secret
sent in the ``X-Zoho-Secret`` header.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import deploy_automation_service as automation_svc
from ..services import zoho_fields_service as fields_svc
from ..services import zoho_layout_service as layout_svc
from ..services import zoho_sync_service as svc
from ..services.deploy_automation_service import AutomationError
from ..services.zoho_client import ZohoError

zoho_bp = Blueprint("zoho", __name__, url_prefix="/api/zoho")


@zoho_bp.route("/config", methods=["GET"])
@require_permission("zoho:view")
def get_config():
    return success_response(svc.get_config_dict())


@zoho_bp.route("/config", methods=["PUT"])
@require_permission("zoho:manage")
def update_config():
    payload = request.get_json(silent=True) or {}
    try:
        data = svc.update_config(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "zoho_integration_updated",
        actor=get_current_user(),
        target_type="zoho_integration",
        target_id="1",
        details={"enabled": data.get("enabled"), "layoutId": data.get("layoutId")},
    )
    return success_response(data)


@zoho_bp.route("/test", methods=["POST"])
@require_permission("zoho:manage")
def test_connection():
    return success_response(svc.test_connection())


@zoho_bp.route("/sync", methods=["POST"])
@require_permission("zoho:manage")
def sync_now():
    try:
        result = svc.sync_now()
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "zoho_sync_run",
        actor=get_current_user(),
        target_type="zoho_integration",
        target_id="1",
        details={"status": result.get("status"), "count": result.get("count")},
    )
    return success_response(result)


@zoho_bp.route("/preview", methods=["GET"])
@require_permission("zoho:view")
def preview():
    fresh = request.args.get("fresh") in ("1", "true")
    return success_response(svc.build_preview(fresh=fresh))


# ---------------------------------------------------------------------------
# Dropdown source picker — choose a cluster + which of its namespaces feed the
# Environment dropdown; the Application dropdown then holds those namespaces'
# live deployments. Gated by the existing zoho:* permissions.
# ---------------------------------------------------------------------------

@zoho_bp.route("/source", methods=["GET"])
@require_permission("zoho:view")
def get_source():
    cfg = svc.get_config_dict()
    return success_response(
        {
            "clusterId": cfg.get("sourceClusterId") or "",
            "selectedNamespaces": cfg.get("selectedNamespaces") or [],
            "selectedDeployments": cfg.get("selectedDeployments") or {},
            "customEnvironments": cfg.get("customEnvironments") or [],
            "jobOverrides": cfg.get("jobOverrides") or [],
            "cascadeEnabled": cfg.get("cascadeEnabled"),
        }
    )


@zoho_bp.route("/source", methods=["PUT"])
@require_permission("zoho:manage")
def update_source():
    payload = request.get_json(silent=True) or {}
    cluster_id = payload.get("clusterId")
    namespaces = payload.get("namespaces")
    if namespaces is None:
        namespaces = payload.get("selectedNamespaces")
    if not isinstance(namespaces, list):
        namespaces = []
    deployments = payload.get("deployments")
    if deployments is None:
        deployments = payload.get("selectedDeployments")
    custom_environments = payload.get("customEnvironments")
    if custom_environments is not None and not isinstance(custom_environments, list):
        custom_environments = []
    job_overrides = payload.get("jobOverrides")
    if job_overrides is not None and not isinstance(job_overrides, list):
        job_overrides = []
    try:
        data = svc.set_source(
            cluster_id, namespaces, deployments, custom_environments, job_overrides
        )
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "zoho_source_updated",
        actor=get_current_user(),
        target_type="zoho_integration",
        target_id="1",
        details={
            "clusterId": data.get("sourceClusterId"),
            "namespaces": data.get("selectedNamespaces"),
            "deployments": data.get("selectedDeployments"),
            "customEnvironments": data.get("customEnvironments"),
            "jobOverrides": data.get("jobOverrides"),
        },
    )
    return success_response(data)


@zoho_bp.route("/source/clusters", methods=["GET"])
@require_permission("zoho:view")
def source_clusters():
    return success_response({"items": svc.list_source_clusters()})


@zoho_bp.route("/source/clusters/<cluster_id>/namespaces", methods=["GET"])
@require_permission("zoho:view")
def source_namespaces(cluster_id: str):
    try:
        names = svc.list_source_namespaces(cluster_id)
    except ValueError as exc:
        return error_response(str(exc), 502)
    return success_response({"clusterId": cluster_id, "namespaces": names})


@zoho_bp.route("/source/deployments", methods=["GET"])
@require_permission("zoho:view")
def source_deployments():
    cluster_id = request.args.get("clusterId", "")
    ns_arg = request.args.get("namespaces", "")
    namespaces = [n.strip() for n in ns_arg.split(",") if n.strip()]
    try:
        data = svc.preview_source_deployments(cluster_id, namespaces)
    except ValueError as exc:
        return error_response(str(exc), 502)
    return success_response(data)


# ---------------------------------------------------------------------------
# Layout field editor — view sections/fields, manage dropdowns, add/edit fields.
# All operations are pinned to the DevOps Request layout by the client guard.
# ---------------------------------------------------------------------------

@zoho_bp.route("/layout", methods=["GET"])
@require_permission("zoho:view")
def get_layout():
    fresh = request.args.get("fresh") in ("1", "true")
    try:
        return success_response(fields_svc.get_layout_structure(fresh=fresh))
    except ZohoError as exc:
        return error_response(str(exc), 502)


@zoho_bp.route("/fields/<field_id>/options", methods=["PUT"])
@require_permission("zoho:manage")
def set_field_options(field_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        data = fields_svc.set_field_options(field_id, payload)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        return error_response(str(exc), 502)
    log_audit(
        "zoho_field_options_updated",
        actor=get_current_user(),
        target_type="zoho_field",
        target_id=str(field_id),
        details={"count": len(data.get("allowedValues") or [])},
    )
    return success_response(data)


@zoho_bp.route("/fields/<field_id>", methods=["PATCH"])
@require_permission("zoho:manage")
def update_field(field_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        data = fields_svc.update_field(field_id, payload)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        return error_response(str(exc), 502)
    log_audit(
        "zoho_field_updated",
        actor=get_current_user(),
        target_type="zoho_field",
        target_id=str(field_id),
        details={"label": data.get("label"), "required": data.get("required")},
    )
    return success_response(data)


@zoho_bp.route("/fields", methods=["POST"])
@require_permission("zoho:manage")
def create_field():
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = fields_svc.create_field(payload, actor=_actor_name(actor))
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        # 403 here almost always = token lacks Desk.settings.CREATE.
        return error_response(str(exc), 502)
    log_audit(
        "zoho_field_created",
        actor=actor,
        target_type="zoho_field",
        target_id=str(data.get("id") or ""),
        details={
            "label": data.get("label"),
            "type": data.get("type"),
            "sectionName": data.get("sectionName"),
            "warnings": data.get("warnings"),
        },
    )
    return success_response(data, status_code=201)


# ---------------------------------------------------------------------------
# Sections + field placement.
#
# Zoho only exposes section editing through a WHOLE-LAYOUT replace, so both
# routes go through zoho_layout_service's guarded writer and both are gated by
# ZOHO_LAYOUT_WRITE_ENABLED. `/layout/plan` is the read-only dry-run that shows
# the exact body first.
# ---------------------------------------------------------------------------

def _actor_name(actor) -> str:
    return str(getattr(actor, "username", "") or getattr(actor, "email", "") or "")


def _layout_write_error(exc: Exception, status: int = 400):
    return error_response(str(exc), status)


@zoho_bp.route("/layout/plan", methods=["POST"])
@require_permission("zoho:manage")
def plan_layout():
    """Dry-run a layout change: returns the exact PATCH body, a diff, and warnings."""
    payload = request.get_json(silent=True) or {}
    mutations = payload.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        name = str(payload.get("sectionName") or "").strip()
        if not name:
            return error_response("Send either 'mutations' or a 'sectionName'.", 400)
        try:
            return success_response(fields_svc.plan_section(name, payload.get("fieldId")))
        except layout_svc.LayoutWriteError as exc:
            return _layout_write_error(exc)
        except LookupError as exc:
            return error_response(str(exc), 404)
        except ZohoError as exc:
            return error_response(str(exc), 502)
    try:
        return success_response(layout_svc.plan_layout_write(mutations))
    except layout_svc.LayoutWriteError as exc:
        return _layout_write_error(exc)
    except ZohoError as exc:
        return error_response(str(exc), 502)


@zoho_bp.route("/sections", methods=["POST"])
@require_permission("zoho:manage")
def create_section():
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = fields_svc.create_section(
            payload.get("name"), payload.get("fieldId"), actor=_actor_name(actor)
        )
    except layout_svc.LayoutWriteError as exc:
        return _layout_write_error(exc)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except PermissionError as exc:
        return error_response(str(exc), 409)
    except ZohoError as exc:
        return error_response(str(exc), 502)
    log_audit(
        "zoho_section_created",
        actor=actor,
        target_type="zoho_layout",
        target_id=str(data.get("layoutId") or ""),
        details={
            "name": data.get("name"),
            "firstFieldId": data.get("firstFieldId"),
            "diff": data.get("diff"),
        },
    )
    return success_response(data, status_code=201)


@zoho_bp.route("/fields/<field_id>/section", methods=["PUT"])
@require_permission("zoho:manage")
def move_field_section(field_id: str):
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = fields_svc.move_field_to_section(
            field_id, str(payload.get("sectionName") or ""), actor=_actor_name(actor)
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except layout_svc.LayoutWriteError as exc:
        return _layout_write_error(exc)
    except PermissionError as exc:
        return error_response(str(exc), 409)
    except ZohoError as exc:
        return error_response(str(exc), 502)
    log_audit(
        "zoho_field_placed",
        actor=actor,
        target_type="zoho_field",
        target_id=str(field_id),
        details={"sectionName": data.get("sectionName")},
    )
    return success_response(data)


# ---------------------------------------------------------------------------
# Text -> Picklist conversion. Zoho cannot retype a field, so this creates a new
# one; GET returns the impact report to show BEFORE that happens.
# ---------------------------------------------------------------------------

@zoho_bp.route("/fields/<field_id>/convert", methods=["GET"])
@require_permission("zoho:view")
def plan_convert_field(field_id: str):
    try:
        return success_response(fields_svc.plan_field_conversion(field_id))
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        return error_response(str(exc), 502)


@zoho_bp.route("/fields/<field_id>/convert", methods=["POST"])
@require_permission("zoho:manage")
def convert_field(field_id: str):
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = fields_svc.convert_field_to_picklist(
            field_id, payload, actor=_actor_name(actor)
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        return error_response(str(exc), 502)
    log_audit(
        "zoho_field_converted",
        actor=actor,
        target_type="zoho_field",
        target_id=str(field_id),
        details={
            "oldApiName": (data.get("oldField") or {}).get("apiName"),
            "newFieldId": (data.get("newField") or {}).get("id"),
            "newApiName": (data.get("newField") or {}).get("apiName"),
            "retired": data.get("retired"),
            "repointed": data.get("repointed"),
        },
    )
    return success_response(data, status_code=201)


# ---------------------------------------------------------------------------
# Option-source bindings — bind any picklist to a live KubeSight source.
#
# Preview is a POST because it carries the binding being edited (and because
# client.js de-dupes concurrent identical GETs, which would collapse two
# previews of different drafts into one).
# ---------------------------------------------------------------------------

@zoho_bp.route("/option-sources", methods=["GET"])
@require_permission("zoho:view")
def option_sources():
    return success_response(fields_svc.list_option_sources())


@zoho_bp.route("/fields/<field_id>/binding", methods=["GET"])
@require_permission("zoho:view")
def get_field_binding(field_id: str):
    data = fields_svc.get_field_binding(field_id)
    if data is None:
        return error_response("That field has no option source.", 404)
    return success_response(data)


@zoho_bp.route("/fields/<field_id>/binding", methods=["PUT"])
@require_permission("zoho:manage")
def set_field_binding(field_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        data = fields_svc.set_field_binding(field_id, payload)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        return error_response(str(exc), 502)
    log_audit(
        "zoho_field_binding_set",
        actor=get_current_user(),
        target_type="zoho_field",
        target_id=str(field_id),
        details={
            "sourceKind": data.get("sourceKind"),
            "parentFieldId": data.get("parentFieldId"),
            "enabled": data.get("enabled"),
        },
    )
    return success_response(data)


@zoho_bp.route("/fields/<field_id>/binding", methods=["DELETE"])
@require_permission("zoho:manage")
def delete_field_binding(field_id: str):
    if not fields_svc.delete_field_binding(field_id):
        return error_response("That field has no option source.", 404)
    log_audit(
        "zoho_field_binding_deleted",
        actor=get_current_user(),
        target_type="zoho_field",
        target_id=str(field_id),
    )
    return success_response({"deleted": True, "fieldId": str(field_id)})


@zoho_bp.route("/fields/<field_id>/binding/preview", methods=["POST"])
@require_permission("zoho:view")
def preview_field_binding(field_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(fields_svc.preview_field_binding(field_id, payload))
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        return error_response(str(exc), 502)


@zoho_bp.route("/inbound-tickets", methods=["GET"])
@require_permission("zoho:view")
def inbound_tickets():
    limit = request.args.get("limit", 50)
    return success_response({"items": svc.list_inbound_tickets(limit=limit)})


# ---------------------------------------------------------------------------
# Deploy automation — Jenkins router connection + per-ticket automation runs.
# ---------------------------------------------------------------------------

@zoho_bp.route("/jenkins", methods=["GET"])
@require_permission("zoho:view")
def get_jenkins():
    return success_response(automation_svc.get_jenkins_dict())


@zoho_bp.route("/jenkins", methods=["PUT"])
@require_permission("zoho:manage")
def update_jenkins():
    payload = request.get_json(silent=True) or {}
    try:
        data = automation_svc.update_jenkins(payload)
    except AutomationError as exc:
        return error_response(str(exc), exc.status)
    log_audit(
        "jenkins_connection_updated",
        actor=get_current_user(),
        target_type="jenkins_connection",
        target_id="1",
        details={"enabled": data.get("enabled"), "routerJobPath": data.get("routerJobPath")},
    )
    return success_response(data)


@zoho_bp.route("/jenkins/test", methods=["POST"])
@require_permission("zoho:manage")
def test_jenkins():
    return success_response(automation_svc.test_jenkins())


@zoho_bp.route("/automation/runs", methods=["GET"])
@require_permission("zoho:view")
def list_automation_runs():
    limit = request.args.get("limit", 50, type=int)
    return success_response({"items": automation_svc.list_runs(limit=limit)})


@zoho_bp.route("/automation/runs", methods=["POST"])
@require_permission("zoho:manage")
def start_automation_run():
    payload = request.get_json(silent=True) or {}
    ticket_record_id = payload.get("ticketRecordId")
    if not ticket_record_id:
        return error_response("ticketRecordId is required.", 400)
    try:
        data = automation_svc.start_run(int(ticket_record_id), user=get_current_user(), auto=False)
    except (TypeError, ValueError):
        return error_response("ticketRecordId must be a number.", 400)
    except AutomationError as exc:
        return error_response(str(exc), exc.status)
    return success_response(data, status_code=201)


@zoho_bp.route("/automation/runs/<int:run_id>/cancel", methods=["POST"])
@require_permission("zoho:manage")
def cancel_automation_run(run_id: int):
    try:
        data = automation_svc.cancel_run(run_id, user=get_current_user())
    except AutomationError as exc:
        return error_response(str(exc), exc.status)
    return success_response(data)


@zoho_bp.route("/inbound-tickets/<int:record_id>", methods=["DELETE"])
@require_permission("zoho:manage")
def delete_inbound_ticket(record_id: int):
    info = svc.delete_inbound_ticket(record_id)
    if info is None:
        return error_response("Inbound ticket not found.", 404)
    log_audit(
        "zoho_inbound_ticket_deleted",
        actor=get_current_user(),
        target_type="zoho_inbound_ticket",
        target_id=str(record_id),
        details=info,
    )
    return success_response({"deleted": True, **info})


@zoho_bp.route("/inbound", methods=["POST"])
def inbound_webhook():
    """Zoho -> KubeSight: a DevOps Request ticket. Secret-verified, not session auth."""
    provided = request.headers.get("X-Zoho-Secret") or request.args.get("secret")
    if not svc.verify_inbound_secret(provided):
        return error_response("Invalid or missing webhook secret.", 401)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error_response("Expected a JSON object body.", 400)
    result = svc.resolve_inbound(payload)
    log_audit(
        "zoho_inbound_ticket",
        actor=None,
        target_type="zoho_inbound_ticket",
        target_id=str(result.get("recordId") or ""),
        details={
            "resolved": result.get("resolved"),
            "targetId": result.get("targetId"),
            "deploymentName": result.get("deploymentName"),
            "tag": result.get("tag"),
        },
    )
    # Always 200 so Zoho's workflow doesn't retry-storm; the resolution outcome
    # (resolved/error) is in the body and persisted for the operator to review.
    return success_response(result)
