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
from ..services import zoho_fields_service as fields_svc
from ..services import zoho_sync_service as svc
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
    return success_response(svc.build_preview())


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
    data = svc.set_source(cluster_id, namespaces, deployments)
    log_audit(
        "zoho_source_updated",
        actor=get_current_user(),
        target_type="zoho_integration",
        target_id="1",
        details={
            "clusterId": data.get("sourceClusterId"),
            "namespaces": data.get("selectedNamespaces"),
            "deployments": data.get("selectedDeployments"),
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
    try:
        return success_response(fields_svc.get_layout_structure())
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
    try:
        data = fields_svc.create_field(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except ZohoError as exc:
        # 403 here almost always = token lacks Desk.settings.CREATE.
        return error_response(str(exc), 502)
    log_audit(
        "zoho_field_created",
        actor=get_current_user(),
        target_type="zoho_field",
        target_id=str(data.get("id") or ""),
        details={"label": data.get("label"), "type": data.get("type")},
    )
    return success_response(data, status_code=201)


@zoho_bp.route("/inbound-tickets", methods=["GET"])
@require_permission("zoho:view")
def inbound_tickets():
    limit = request.args.get("limit", 50)
    return success_response({"items": svc.list_inbound_tickets(limit=limit)})


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
