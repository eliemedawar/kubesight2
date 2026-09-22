"""Merge checks API: the quality gate policy, per-service config, and the hook.

Every route here is session authenticated and permission gated EXCEPT the
inbound webhook, which cannot be: Bitbucket holds no KubeSight session and
never will. That one endpoint authenticates the caller with a per-service
shared secret instead, the same arrangement the ticketing inbound webhook uses,
and it is kept in its own clearly labelled section at the bottom of this file
so nobody adds a route beside it by accident.

The webhook always answers 200 once the secret checks out, even when it decided
to do nothing. A webhook host reads a non-2xx as "retry", and a payload that is
being ignored on purpose — wrong branch, wrong event — would then be retried
forever. What happened is in the body, and on the Merge Checks tab.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..db import db
from ..models_ci import CiService
from ..response import error_response, success_response
from ..services.ci import merge_checks as merge_checks_service
from ..services.ci.merge_checks import policy as policy_service

ci_merge_checks_bp = Blueprint("ci_merge_checks", __name__, url_prefix="/api/ci")

_USER_ERRORS = (merge_checks_service.MergeCheckError, policy_service.PolicyError)


def _payload():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _service(service_id: int) -> CiService:
    row = db.session.get(CiService, int(service_id))
    if row is None:
        raise LookupError("Service not found.")
    return row


# ---------------------------------------------------------------------------
# The installation-wide quality gate (Settings -> Merge checks)
# ---------------------------------------------------------------------------

def _policy_payload():
    row = policy_service.get_policy()
    return {
        "enabledByDefault": bool(row.enabled_by_default),
        "gate": policy_service.gate_payload(row),
        # What a service that overrides nothing is actually judged against —
        # the policy's own numbers filled in with the built-in defaults. Shown
        # beside the form so "leave blank" has a visible consequence.
        "effectiveGate": policy_service.resolve_gate(None, row),
        "defaults": policy_service.GATE_DEFAULTS,
        "tools": list(merge_checks_service.MERGE_CHECK_TOOLS),
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


@ci_merge_checks_bp.route("/merge-checks/policy", methods=["GET"])
@require_permission("ci_merge_checks:view")
def get_policy():
    return success_response(_policy_payload())


@ci_merge_checks_bp.route("/merge-checks/policy", methods=["PUT"])
@require_permission("ci_merge_checks:manage")
def update_policy():
    payload = _payload()
    row = policy_service.get_policy()
    try:
        policy_service.apply_gate_fields(row, payload)
    except policy_service.PolicyError as exc:
        return error_response(str(exc), 400)
    if "enabledByDefault" in payload:
        row.enabled_by_default = bool(payload.get("enabledByDefault"))
    actor = get_current_user()
    row.updated_by_user_id = getattr(actor, "id", None)
    db.session.add(row)
    db.session.commit()

    from ..audit import log_audit

    log_audit(
        "ci_merge_check_policy_saved",
        actor=actor,
        target_type="ci_merge_check_policy",
        target_id="1",
        details=policy_service.gate_payload(row),
    )
    return success_response(_policy_payload())


# ---------------------------------------------------------------------------
# One service's merge checks
# ---------------------------------------------------------------------------

@ci_merge_checks_bp.route("/services/<int:service_id>/merge-checks", methods=["GET"])
@require_permission("ci_merge_checks:view")
def get_service_config(service_id: int):
    try:
        service = _service(service_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    return success_response(merge_checks_service.config_payload(service))


@ci_merge_checks_bp.route("/services/<int:service_id>/merge-checks", methods=["PUT"])
@require_permission("ci_merge_checks:manage")
def save_service_config(service_id: int):
    try:
        service = _service(service_id)
        data = merge_checks_service.save_config(
            service, _payload(), actor=get_current_user()
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except _USER_ERRORS as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_merge_checks_bp.route(
    "/services/<int:service_id>/merge-checks/secret", methods=["GET"]
)
@require_permission("ci_merge_checks:manage")
def reveal_secret(service_id: int):
    """The shared secret, in plaintext, for pasting into Bitbucket.

    Gated on `manage` rather than `view` and audited: this is the credential
    that lets anybody trigger builds on this service.
    """
    try:
        service = _service(service_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    value = merge_checks_service.inbound_secret(service)
    if not value:
        return error_response("No webhook secret has been generated yet.", 404)

    from ..audit import log_audit

    log_audit(
        "ci_merge_checks_secret_revealed",
        actor=get_current_user(),
        target_type="ci_merge_check_config",
        target_id=str(service.id),
        details={"service": service.slug},
    )
    return success_response({"secret": value})


@ci_merge_checks_bp.route(
    "/services/<int:service_id>/merge-checks/secret", methods=["POST"]
)
@require_permission("ci_merge_checks:manage")
def rotate_secret(service_id: int):
    try:
        service = _service(service_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    value = merge_checks_service.rotate_secret(service, actor=get_current_user())
    return success_response(
        {
            "secret": value,
            "message": (
                "The previous secret stopped working immediately. Update the "
                "webhook in Bitbucket before the next pull request."
            ),
        }
    )


@ci_merge_checks_bp.route(
    "/services/<int:service_id>/merge-checks/enforcement", methods=["GET"]
)
@require_permission("ci_merge_checks:view")
def enforcement(service_id: int):
    """Ask Bitbucket whether a failed check would actually stop the merge.

    Separate from the configuration payload because it is a live third-party
    call: the tab has to render, and say what it knows, even when the source
    host is slow or unreachable.
    """
    try:
        service = _service(service_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    return success_response(merge_checks_service.merge_enforcement(service))


@ci_merge_checks_bp.route(
    "/services/<int:service_id>/merge-checks/runs", methods=["GET"]
)
@require_permission("ci_merge_checks:view")
def list_runs(service_id: int):
    try:
        service = _service(service_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    limit = request.args.get("limit", 25)
    items = merge_checks_service.list_checks(service, limit=int(limit or 25))
    return success_response({"items": items, "count": len(items)})


@ci_merge_checks_bp.route("/merge-checks/runs/<int:check_id>", methods=["GET"])
@require_permission("ci_merge_checks:view")
def get_run(check_id: int):
    try:
        row = merge_checks_service.get_check(check_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    return success_response(merge_checks_service.check_payload(row))


@ci_merge_checks_bp.route(
    "/merge-checks/runs/<int:check_id>/redeliver", methods=["POST"]
)
@require_permission("ci_merge_checks:manage")
def redeliver(check_id: int):
    """Try again to hand an already-decided verdict to the source host.

    Re-delivers; never re-decides. A verdict that was reached under last
    month's gate is re-sent as it was, because that is what happened.
    """
    try:
        row = merge_checks_service.get_check(check_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    if not row.verdict:
        return error_response("This check has not reached a verdict yet.", 400)
    row.delivery_state = "pending"
    row.next_delivery_at = None
    row.delivery_attempts = 0
    db.session.add(row)
    db.session.commit()

    from ..services.ci.merge_checks import delivery as delivery_service

    result = delivery_service.deliver(row)
    if not result.get("delivered"):
        return error_response(
            result.get("error") or "The verdict could not be delivered.",
            502,
            merge_checks_service.check_payload(row),
        )
    return success_response(merge_checks_service.check_payload(row))


# ---------------------------------------------------------------------------
# Inbound webhook — secret-verified, NOT session authenticated
# ---------------------------------------------------------------------------

@ci_merge_checks_bp.route("/merge-checks/inbound/<slug>", methods=["POST"])
def inbound(slug: str):
    """A source host announcing a pull request.

    The secret travels in a header by preference and in the query string as a
    fallback, because some webhook UIs cannot set headers. The event key comes
    from Bitbucket's own ``X-Event-Key``; a body with no header is treated as a
    creation, which is the conservative reading — it runs the checks.
    """
    provided = (
        request.headers.get("X-KubeSight-Secret")
        or request.headers.get("X-Hub-Signature")
        or request.args.get("secret", "")
    )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error_response("Expected a JSON object body.", 400)
    event = request.headers.get("X-Event-Key", "") or str(payload.get("eventKey") or "")
    try:
        result = merge_checks_service.ingest(
            slug, payload, event=event, provided_secret=provided
        )
    except PermissionError as exc:
        return error_response(str(exc), 401)
    except LookupError as exc:
        return error_response(str(exc), 404)
    # 200 even when nothing ran: see the module docstring.
    return success_response(result)
