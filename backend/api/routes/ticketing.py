"""Ticketing integrations — one route surface over every provider.

``/api/ticketing/providers`` lists the providers for the tab's cards; everything
else lives under ``/api/ticketing/<provider>/…`` and mirrors, endpoint for
endpoint, what the Zoho tab has always called. The provider is resolved from the
URL into a :class:`~api.services.ticketing.Provider` and the handler calls its
modules, so adding a third provider is a registry entry, not a new blueprint.

Two deliberate properties:

* **The legacy ``/api/zoho/*`` blueprint is untouched.** A live Zoho Desk
  workflow is configured to POST to ``/api/zoho/inbound``; breaking that URL
  would silently stop production deployments. The old routes stay as they are and
  simply call the same services.

* **Unsupported operations answer 501, not 500.** The two providers genuinely
  differ (Jira has no text→dropdown conversion; Desk cannot create a layout
  section), and each difference is declared in the provider's ``capabilities``.
  A route checks the capability and says what is not possible where, instead of
  raising an AttributeError from a module that never had the function.

The inbound webhook is NOT session authenticated — no ticketing system holds a
KubeSight login — so it verifies a shared secret sent in ``X-Ticketing-Secret``
(or the provider's own legacy header) instead.
"""

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services import deploy_automation_service as automation_svc
from ..services import ticketing
from ..services.deploy_automation_service import AutomationError

ticketing_bp = Blueprint("ticketing", __name__, url_prefix="/api/ticketing")


# ---------------------------------------------------------------------------
# Provider resolution + shared error mapping
# ---------------------------------------------------------------------------

class _UnknownProvider(Exception):
    """Raised by :func:`_provider` so each handler can bail with one line."""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _provider(key: str):
    provider = ticketing.get(key)
    if provider is None:
        raise _UnknownProvider(key)
    return provider


@ticketing_bp.errorhandler(_UnknownProvider)
def _unknown_provider(exc: _UnknownProvider):
    known = ", ".join(ticketing.keys())
    return error_response(f"Unknown ticketing provider '{exc.key}'. Known: {known}.", 404)


def _actor_name(actor) -> str:
    return str(getattr(actor, "username", "") or getattr(actor, "email", "") or "")


def _unsupported(provider, capability: str, explanation: str):
    """A 501 that names the provider and what it cannot do."""
    return error_response(f"{provider.name} does not support {explanation}.", 501)


def _audit(event: str, provider, target_type: str, target_id: str, details: dict):
    """Audit with the provider folded into the event name and the details.

    Both are needed: the event name is what an operator filters the audit log by,
    and the detail keeps the provider readable on a row whose target id
    ("1", a field id) means nothing on its own.
    """
    log_audit(
        f"ticketing_{provider.key}_{event}",
        actor=get_current_user(),
        target_type=target_type,
        target_id=target_id,
        details={"provider": provider.key, **details},
    )


# ---------------------------------------------------------------------------
# The provider cards
# ---------------------------------------------------------------------------

@ticketing_bp.route("/providers", methods=["GET"])
@require_permission("ticketing:view")
def list_providers():
    """Every provider with enough status for its card (name, configured, last sync)."""
    return success_response({"items": ticketing.describe_all()})


# ---------------------------------------------------------------------------
# Config / test / sync / preview
# ---------------------------------------------------------------------------

@ticketing_bp.route("/<provider_key>/config", methods=["GET"])
@require_permission("ticketing:view")
def get_config(provider_key: str):
    provider = _provider(provider_key)
    return success_response(
        {**provider.sync.get_config_dict(), "provider": provider.key,
         "capabilities": dict(provider.capabilities)}
    )


@ticketing_bp.route("/<provider_key>/config", methods=["PUT"])
@require_permission("ticketing:manage")
def update_config(provider_key: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    try:
        data = provider.sync.update_config(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit(
        "integration_updated",
        provider,
        f"{provider.key}_integration",
        "1",
        {"enabled": data.get("enabled"), "layoutId": data.get("layoutId")},
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/test", methods=["POST"])
@require_permission("ticketing:manage")
def test_connection(provider_key: str):
    provider = _provider(provider_key)
    return success_response(provider.sync.test_connection())


@ticketing_bp.route("/<provider_key>/sync", methods=["POST"])
@require_permission("ticketing:manage")
def sync_now(provider_key: str):
    provider = _provider(provider_key)
    try:
        result = provider.sync.sync_now()
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit(
        "sync_run",
        provider,
        f"{provider.key}_integration",
        "1",
        {"status": result.get("status"), "count": result.get("count")},
    )
    return success_response(result)


@ticketing_bp.route("/<provider_key>/preview", methods=["GET"])
@require_permission("ticketing:view")
def preview(provider_key: str):
    provider = _provider(provider_key)
    fresh = request.args.get("fresh") in ("1", "true")
    return success_response(provider.sync.build_preview(fresh=fresh))


# ---------------------------------------------------------------------------
# Dropdown source picker — one deploy surface per provider.
#
# Writes land on the addressed provider's ticketing_targets row, so each tab
# publishes its own slice of the estate: narrowing namespaces on the Jira tab
# leaves what Zoho publishes untouched.
# ---------------------------------------------------------------------------

@ticketing_bp.route("/<provider_key>/source", methods=["GET"])
@require_permission("ticketing:view")
def get_source(provider_key: str):
    provider = _provider(provider_key)
    cfg = provider.sync.get_config_dict()
    return success_response(
        {
            "clusterId": cfg.get("sourceClusterId") or "",
            "selectedNamespaces": cfg.get("selectedNamespaces") or [],
            "selectedDeployments": cfg.get("selectedDeployments") or {},
            "customEnvironments": cfg.get("customEnvironments") or [],
            "jobOverrides": cfg.get("jobOverrides") or [],
            "cascadeEnabled": cfg.get("cascadeEnabled"),
            # Each provider owns its selection; kept in the payload (rather than
            # dropped) so a client written against the shared-source API sees the
            # change instead of reading a missing key as "shared".
            "shared": False,
        }
    )


@ticketing_bp.route("/<provider_key>/source", methods=["PUT"])
@require_permission("ticketing:manage")
def update_source(provider_key: str):
    provider = _provider(provider_key)
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
        data = provider.sync.set_source(
            cluster_id, namespaces, deployments, custom_environments, job_overrides
        )
    except ValueError as exc:
        return error_response(str(exc), 400)
    _audit(
        "source_updated",
        provider,
        "ticketing_deploy_config",
        # The row is keyed by provider now, so that is the readable target id.
        provider.key,
        {
            "clusterId": data.get("sourceClusterId"),
            "namespaces": data.get("selectedNamespaces"),
            "deployments": data.get("selectedDeployments"),
            "customEnvironments": data.get("customEnvironments"),
            "jobOverrides": data.get("jobOverrides"),
        },
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/source/clusters", methods=["GET"])
@require_permission("ticketing:view")
def source_clusters(provider_key: str):
    provider = _provider(provider_key)
    return success_response({"items": provider.sync.list_source_clusters()})


@ticketing_bp.route("/<provider_key>/source/clusters/<cluster_id>/namespaces", methods=["GET"])
@require_permission("ticketing:view")
def source_namespaces(provider_key: str, cluster_id: str):
    provider = _provider(provider_key)
    try:
        names = provider.sync.list_source_namespaces(cluster_id)
    except ValueError as exc:
        return error_response(str(exc), 502)
    return success_response({"clusterId": cluster_id, "namespaces": names})


@ticketing_bp.route("/<provider_key>/source/deployments", methods=["GET"])
@require_permission("ticketing:view")
def source_deployments(provider_key: str):
    provider = _provider(provider_key)
    cluster_id = request.args.get("clusterId", "")
    ns_arg = request.args.get("namespaces", "")
    namespaces = [n.strip() for n in ns_arg.split(",") if n.strip()]
    try:
        data = provider.sync.preview_source_deployments(cluster_id, namespaces)
    except ValueError as exc:
        return error_response(str(exc), 502)
    return success_response(data)


# ---------------------------------------------------------------------------
# Form editor — sections/fields, option lists, field CRUD.
#
# "layout" in the paths is the provider-neutral word for the form: a Zoho Desk
# layout, a Jira screen. The path is kept identical to the Zoho one so the same
# editor component serves both.
# ---------------------------------------------------------------------------

@ticketing_bp.route("/<provider_key>/fields/<field_id>", methods=["GET"])
@require_permission("ticketing:view")
def get_field(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    try:
        return success_response(provider.fields.get_field(field_id))
    except LookupError as exc:
        return error_response(str(exc), 404)
    except provider.error as exc:
        return error_response(str(exc), 502)


@ticketing_bp.route("/<provider_key>/layout", methods=["GET"])
@require_permission("ticketing:view")
def get_layout(provider_key: str):
    provider = _provider(provider_key)
    fresh = request.args.get("fresh") in ("1", "true")
    try:
        return success_response(provider.fields.get_layout_structure(fresh=fresh))
    except provider.error as exc:
        return error_response(str(exc), 502)


@ticketing_bp.route("/<provider_key>/fields/<field_id>/options", methods=["PUT"])
@require_permission("ticketing:manage")
def set_field_options(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    try:
        data = provider.fields.set_field_options(field_id, payload)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "field_options_updated",
        provider,
        "ticketing_field",
        str(field_id),
        {"count": len(data.get("allowedValues") or [])},
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/fields/<field_id>", methods=["PATCH"])
@require_permission("ticketing:manage")
def update_field(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    try:
        data = provider.fields.update_field(field_id, payload)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "field_updated",
        provider,
        "ticketing_field",
        str(field_id),
        {"label": data.get("label"), "required": data.get("required")},
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/fields/<field_id>", methods=["DELETE"])
@require_permission("ticketing:manage")
def delete_field(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    if not provider.capabilities.get("deleteField"):
        return _unsupported(provider, "deleteField", "deleting a field through its API")
    # Jira splits "take off the form" from "delete the field"; Zoho ignores it.
    payload = request.get_json(silent=True) or {}
    try:
        data = provider.fields.delete_field(field_id, payload)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 409)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "field_deleted",
        provider,
        "ticketing_field",
        str(field_id),
        {
            "label": data.get("label"),
            "apiName": data.get("apiName"),
            "deleted": data.get("deleted", True),
        },
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/fields", methods=["POST"])
@require_permission("ticketing:manage")
def create_field(provider_key: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = provider.fields.create_field(payload, actor=_actor_name(actor))
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        # 403 here is almost always a missing admin scope on the credential.
        return error_response(str(exc), 502)
    _audit(
        "field_created",
        provider,
        "ticketing_field",
        str(data.get("id") or ""),
        {
            "label": data.get("label"),
            "type": data.get("type"),
            "sectionName": data.get("sectionName"),
            "warnings": data.get("warnings"),
        },
    )
    return success_response(data, status_code=201)


# ---------------------------------------------------------------------------
# Sections + field placement
# ---------------------------------------------------------------------------

@ticketing_bp.route("/<provider_key>/layout/plan", methods=["POST"])
@require_permission("ticketing:manage")
def plan_layout(provider_key: str):
    """Dry-run a structural change before it is applied.

    For Zoho this returns the exact whole-layout PATCH body plus a diff — the
    only safe way to review a write that replaces the entire form. Jira applies
    each change individually, so its plan is a statement of intent.
    """
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    mutations = payload.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        name = str(payload.get("sectionName") or "").strip()
        if not name:
            return error_response("Send either 'mutations' or a 'sectionName'.", 400)
        try:
            return success_response(provider.fields.plan_section(name, payload.get("fieldId")))
        except LookupError as exc:
            return error_response(str(exc), 404)
        except ValueError as exc:
            return error_response(str(exc), 400)
        except provider.error as exc:
            return error_response(str(exc), 502)
    if not provider.capabilities.get("layoutPlan"):
        return _unsupported(
            provider, "layoutPlan", "whole-form mutation plans (it applies each change directly)"
        )
    from ..services import zoho_layout_service as layout_svc

    try:
        return success_response(layout_svc.plan_layout_write(mutations))
    except layout_svc.LayoutWriteError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)


@ticketing_bp.route("/<provider_key>/layout/snapshots", methods=["GET"])
@require_permission("ticketing:view")
def list_layout_snapshots(provider_key: str):
    provider = _provider(provider_key)
    if not provider.capabilities.get("layoutRecovery"):
        return _unsupported(provider, "layoutRecovery", "layout recovery snapshots")
    from ..services import zoho_layout_service as layout_svc

    limit = request.args.get("limit", 10, type=int)
    return success_response({"items": layout_svc.list_layout_snapshots(limit)})


@ticketing_bp.route(
    "/<provider_key>/layout/snapshots/<int:snapshot_id>/plan", methods=["POST"]
)
@require_permission("ticketing:view")
def plan_layout_snapshot_restore(provider_key: str, snapshot_id: int):
    provider = _provider(provider_key)
    if not provider.capabilities.get("layoutRecovery"):
        return _unsupported(provider, "layoutRecovery", "layout recovery snapshots")
    from ..services import zoho_layout_service as layout_svc

    try:
        return success_response(layout_svc.plan_snapshot_restore(snapshot_id))
    except LookupError as exc:
        return error_response(str(exc), 404)
    except layout_svc.LayoutWriteError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)


@ticketing_bp.route(
    "/<provider_key>/layout/snapshots/<int:snapshot_id>/restore", methods=["POST"]
)
@require_permission("ticketing:manage")
def restore_layout_snapshot(provider_key: str, snapshot_id: int):
    provider = _provider(provider_key)
    if not provider.capabilities.get("layoutRecovery"):
        return _unsupported(provider, "layoutRecovery", "layout recovery snapshots")
    from ..services import zoho_layout_service as layout_svc

    actor = get_current_user()
    try:
        data = layout_svc.restore_layout_snapshot(snapshot_id, actor=_actor_name(actor))
    except LookupError as exc:
        return error_response(str(exc), 404)
    except PermissionError as exc:
        return error_response(str(exc), 409)
    except layout_svc.LayoutWriteError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "layout_snapshot_restored",
        provider,
        "ticketing_layout",
        str(data.get("layoutId") or ""),
        {"snapshotId": snapshot_id, "diff": data.get("diff")},
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/sections", methods=["POST"])
@require_permission("ticketing:manage")
def create_section(provider_key: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = provider.fields.create_section(
            payload.get("name"), payload.get("fieldId"), actor=_actor_name(actor)
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except PermissionError as exc:
        return error_response(str(exc), 409)
    except ValueError as exc:
        # Includes Zoho's LayoutWriteError, which subclasses ValueError.
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "section_created",
        provider,
        "ticketing_layout",
        str(data.get("layoutId") or ""),
        {"name": data.get("name"), "diff": data.get("diff")},
    )
    return success_response(data, status_code=201)


@ticketing_bp.route("/<provider_key>/sections/<section_id>", methods=["PATCH"])
@require_permission("ticketing:manage")
def rename_section(provider_key: str, section_id: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = provider.fields.rename_section(
            section_id, payload.get("name"), actor=_actor_name(actor)
        )
    except PermissionError as exc:
        return error_response(str(exc), 409)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "section_renamed",
        provider,
        "ticketing_layout_section",
        str(section_id),
        {"name": data.get("name"), "diff": data.get("diff")},
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/fields/<field_id>/section", methods=["PUT"])
@require_permission("ticketing:manage")
def move_field_section(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = provider.fields.move_field_to_section(
            field_id, str(payload.get("sectionName") or ""), actor=_actor_name(actor)
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except PermissionError as exc:
        return error_response(str(exc), 409)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "field_placed",
        provider,
        "ticketing_field",
        str(field_id),
        {"sectionName": data.get("sectionName")},
    )
    return success_response(data)


# ---------------------------------------------------------------------------
# Text -> dropdown conversion (Zoho only; Jira cannot retype a field either, but
# has no create-replacement-and-repoint flow built for it).
# ---------------------------------------------------------------------------

@ticketing_bp.route("/<provider_key>/fields/<field_id>/convert", methods=["GET"])
@require_permission("ticketing:view")
def plan_convert_field(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    if not provider.capabilities.get("convertField"):
        return _unsupported(provider, "convertField", "converting a text field to a dropdown")
    try:
        return success_response(provider.fields.plan_field_conversion(field_id))
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)


@ticketing_bp.route("/<provider_key>/fields/<field_id>/convert", methods=["POST"])
@require_permission("ticketing:manage")
def convert_field(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    if not provider.capabilities.get("convertField"):
        return _unsupported(provider, "convertField", "converting a text field to a dropdown")
    payload = request.get_json(silent=True) or {}
    actor = get_current_user()
    try:
        data = provider.fields.convert_field_to_picklist(
            field_id, payload, actor=_actor_name(actor)
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "field_converted",
        provider,
        "ticketing_field",
        str(field_id),
        {
            "oldApiName": (data.get("oldField") or {}).get("apiName"),
            "newFieldId": (data.get("newField") or {}).get("id"),
            "newApiName": (data.get("newField") or {}).get("apiName"),
            "retired": data.get("retired"),
            "repointed": data.get("repointed"),
        },
    )
    return success_response(data, status_code=201)


# ---------------------------------------------------------------------------
# Option-source bindings — bind any dropdown to a live KubeSight source.
#
# Preview is a POST because it carries the unsaved binding being edited (and
# because the API client de-dupes concurrent identical GETs, which would collapse
# two previews of different drafts into one).
# ---------------------------------------------------------------------------

@ticketing_bp.route("/<provider_key>/option-sources", methods=["GET"])
@require_permission("ticketing:view")
def option_sources(provider_key: str):
    provider = _provider(provider_key)
    return success_response(provider.fields.list_option_sources())


@ticketing_bp.route("/<provider_key>/fields/<field_id>/binding", methods=["GET"])
@require_permission("ticketing:view")
def get_field_binding(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    data = provider.fields.get_field_binding(field_id)
    if data is None:
        return error_response("This field has no option source.", 404)
    return success_response(data)


@ticketing_bp.route("/<provider_key>/fields/<field_id>/binding", methods=["PUT"])
@require_permission("ticketing:manage")
def set_field_binding(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    try:
        data = provider.fields.set_field_binding(field_id, payload)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)
    _audit(
        "field_binding_set",
        provider,
        "ticketing_field",
        str(field_id),
        {"sourceKind": data.get("sourceKind"), "parentFieldId": data.get("parentFieldId")},
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/fields/<field_id>/binding", methods=["DELETE"])
@require_permission("ticketing:manage")
def delete_field_binding(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    if not provider.fields.delete_field_binding(field_id):
        return error_response("This field has no option source.", 404)
    _audit("field_binding_deleted", provider, "ticketing_field", str(field_id), {})
    return success_response({"deleted": True, "fieldId": str(field_id)})


@ticketing_bp.route("/<provider_key>/fields/<field_id>/binding/preview", methods=["POST"])
@require_permission("ticketing:view")
def preview_field_binding(provider_key: str, field_id: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    try:
        return success_response(provider.fields.preview_field_binding(field_id, payload))
    except ValueError as exc:
        return error_response(str(exc), 400)
    except provider.error as exc:
        return error_response(str(exc), 502)


# ---------------------------------------------------------------------------
# Inbound ticket log + deploy automation
#
# Jenkins is one shared connection: a build is a build whoever raised the ticket,
# and two Jenkins configs would just be two ways to get the same job wrong.
# ---------------------------------------------------------------------------

@ticketing_bp.route("/<provider_key>/inbound-tickets", methods=["GET"])
@require_permission("ticketing:view")
def inbound_tickets(provider_key: str):
    provider = _provider(provider_key)
    limit = request.args.get("limit", 50, type=int)
    return success_response({"items": provider.sync.list_inbound_tickets(limit)})


@ticketing_bp.route("/<provider_key>/inbound-tickets/<int:record_id>", methods=["DELETE"])
@require_permission("ticketing:manage")
def delete_inbound_ticket(provider_key: str, record_id: int):
    provider = _provider(provider_key)
    info = provider.sync.delete_inbound_ticket(record_id)
    if info is None:
        return error_response("Inbound ticket not found.", 404)
    _audit("inbound_ticket_deleted", provider, "ticketing_inbound_ticket", str(record_id), info)
    return success_response({"deleted": True, **info})


@ticketing_bp.route("/<provider_key>/jenkins", methods=["GET"])
@require_permission("ticketing:view")
def get_jenkins(provider_key: str):
    _provider(provider_key)
    return success_response(automation_svc.get_jenkins_dict())


@ticketing_bp.route("/<provider_key>/jenkins", methods=["PUT"])
@require_permission("ticketing:manage")
def update_jenkins(provider_key: str):
    provider = _provider(provider_key)
    payload = request.get_json(silent=True) or {}
    try:
        data = automation_svc.update_jenkins(payload)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit(
        "jenkins_connection_updated",
        actor=get_current_user(),
        target_type="jenkins_connection",
        target_id="1",
        details={
            "enabled": data.get("enabled"),
            "routerJobPath": data.get("routerJobPath"),
            "via": provider.key,
        },
    )
    return success_response(data)


@ticketing_bp.route("/<provider_key>/jenkins/test", methods=["POST"])
@require_permission("ticketing:manage")
def test_jenkins(provider_key: str):
    _provider(provider_key)
    return success_response(automation_svc.test_jenkins())


@ticketing_bp.route("/<provider_key>/automation/runs", methods=["GET"])
@require_permission("ticketing:view")
def list_automation_runs(provider_key: str):
    provider = _provider(provider_key)
    limit = request.args.get("limit", 50, type=int)
    return success_response(
        {"items": automation_svc.list_runs(limit=limit, provider=provider.key)}
    )


@ticketing_bp.route("/<provider_key>/automation/runs", methods=["POST"])
@require_permission("ticketing:manage")
def start_automation_run(provider_key: str):
    _provider(provider_key)
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


@ticketing_bp.route("/<provider_key>/automation/runs/<int:run_id>/cancel", methods=["POST"])
@require_permission("ticketing:manage")
def cancel_automation_run(provider_key: str, run_id: int):
    _provider(provider_key)
    try:
        data = automation_svc.cancel_run(run_id, user=get_current_user())
    except AutomationError as exc:
        return error_response(str(exc), exc.status)
    return success_response(data)


# ---------------------------------------------------------------------------
# Inbound webhook — secret-verified, NOT session authenticated
# ---------------------------------------------------------------------------

# Header each provider's own tooling is most likely to already be sending, kept
# alongside the neutral one so a webhook can be pointed here without editing it.
_LEGACY_SECRET_HEADERS = {
    "zoho": "X-Zoho-Secret",
    "jira": "X-Jira-Secret",
}


@ticketing_bp.route("/<provider_key>/inbound", methods=["POST"])
def inbound_webhook(provider_key: str):
    """A ticketing system delivering a DevOps Request. Secret-verified."""
    provider = _provider(provider_key)
    legacy_header = _LEGACY_SECRET_HEADERS.get(provider.key)
    provided = (
        request.headers.get("X-Ticketing-Secret")
        or (request.headers.get(legacy_header) if legacy_header else None)
        or request.args.get("secret")
    )
    if not provider.sync.verify_inbound_secret(provided):
        return error_response("Invalid or missing webhook secret.", 401)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return error_response("Expected a JSON object body.", 400)
    result = provider.sync.resolve_inbound(payload)
    log_audit(
        f"ticketing_{provider.key}_inbound_ticket",
        actor=None,
        target_type="ticketing_inbound_ticket",
        target_id=str(result.get("recordId") or ""),
        details={
            "provider": provider.key,
            "resolved": result.get("resolved"),
            "targetId": result.get("targetId"),
            "deploymentName": result.get("deploymentName"),
            "tag": result.get("tag"),
        },
    )
    # Always 200 so the sender's workflow doesn't retry-storm; the resolution
    # outcome (resolved/error) is in the body and persisted for review.
    return success_response(result)
