"""The integrations hub API.

One address for every outside system: list them, read one, re-test it, switch it
off. The shape each returns is provider-neutral — see
``services/integrations_service.py`` for the contract and why status collapses
to four words.

Authorization is per-integration rather than per-route. The three alert-routing
integrations (SMTP, Slack, webhooks) are admin-only because the routes behind
them are; the rest are gated by their own permission keys. So the blueprint only
requires a session, and each handler asks the service whether this user may see
or change this particular integration. A user with `registries:view` and nothing
else gets a hub containing exactly one card.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_auth
from ..response import error_response, success_response
from ..services import integrations_service as svc

integrations_bp = Blueprint("integrations", __name__, url_prefix="/api/integrations")


def _forbidden():
    return error_response("You do not have access to this resource.", 403)


@integrations_bp.route("", methods=["GET"])
@require_auth
def list_integrations():
    user = get_current_user()
    return success_response({"items": svc.describe_all(user)})


@integrations_bp.route("/<key>", methods=["GET"])
@require_auth
def get_integration(key: str):
    user = get_current_user()
    descriptor = svc.describe_one(user, key)
    if descriptor is None:
        entry = svc.adapter_entry(key)
        return _forbidden() if entry else error_response("Unknown integration.", 404)
    return success_response(descriptor)


@integrations_bp.route("/<key>/test", methods=["POST"])
@require_auth
def test_integration(key: str):
    user = get_current_user()
    entry = svc.adapter_entry(key)
    if entry is None:
        return error_response("Unknown integration.", 404)
    # Testing writes last_test_* and reaches an external host — a viewer may read
    # the result of someone else's test but may not provoke one.
    if not svc.can_manage(user, entry):
        return _forbidden()
    try:
        result = svc.run_test(user, key)
    except (svc.IntegrationActionError, ValueError) as exc:
        return error_response(str(exc), 400)
    log_audit(
        "integration_tested",
        actor=user,
        target_type="integration",
        target_id=key,
        details={"ok": result.get("ok"), "message": result.get("message")},
    )
    return success_response(result)


@integrations_bp.route("/<key>/enabled", methods=["PUT"])
@require_auth
def set_enabled(key: str):
    user = get_current_user()
    entry = svc.adapter_entry(key)
    if entry is None:
        return error_response("Unknown integration.", 404)
    if not svc.can_manage(user, entry):
        return _forbidden()
    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload:
        return error_response("`enabled` is required.", 400)
    enabled = bool(payload.get("enabled"))
    try:
        svc.set_enabled(user, key, enabled)
    except (svc.IntegrationActionError, ValueError) as exc:
        # A provider's own validator can still reject the change; report why
        # rather than letting it surface as an opaque 500.
        return error_response(str(exc), 400)
    log_audit(
        "integration_enabled_changed",
        actor=user,
        target_type="integration",
        target_id=key,
        details={"enabled": enabled},
    )
    return success_response(svc.describe_one(user, key))


@integrations_bp.route("/<key>/activity", methods=["GET"])
@require_auth
def integration_activity(key: str):
    user = get_current_user()
    entry = svc.adapter_entry(key)
    if entry is None:
        return error_response("Unknown integration.", 404)
    if not svc.can_view(user, entry):
        return _forbidden()
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    return success_response({"items": svc.activity_for(key, limit=limit)})
