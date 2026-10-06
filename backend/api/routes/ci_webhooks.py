"""Webhook triggers API: a service's webhooks, their deliveries, and the hook.

Kept beside ``routes/ci.py`` the way schedules are. Permissions follow the
schedules rule: saving a webhook needs ``ci_pipelines:edit`` (standing
configuration of how the service builds) AND ``ci_builds:run`` (it starts
builds as the person who saved it). The secret is guarded by the same pair —
holding it IS the ability to start builds.

Every route here is session authenticated EXCEPT the inbound hook at the
bottom, which cannot be: the sender holds no KubeSight session. It
authenticates with the trigger's own secret instead.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.ci import catalog as catalog_service
from ..services.ci import schedules as schedules_service
from ..services.ci import webhook_triggers as webhooks_service

ci_webhooks_bp = Blueprint("ci_webhooks", __name__, url_prefix="/api/ci")


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@ci_webhooks_bp.errorhandler(LookupError)
def _not_found(exc: LookupError):
    return error_response(str(exc) or "Not found.", 404)


def _trigger(service_id: int, trigger_id: int):
    service = catalog_service.get_service(service_id)
    return webhooks_service.get_trigger(service, trigger_id)


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks", methods=["GET"])
@require_permission("ci_builds:view")
def list_webhooks(service_id: int):
    service = catalog_service.get_service(service_id)
    items = webhooks_service.list_triggers(service)
    return success_response(
        {
            "items": items,
            "count": len(items),
            "pipelines": schedules_service.pipeline_choices(service),
            "defaultBranch": service.default_branch,
            "repositoryProvider": service.repository_provider,
            "repositoryConnected": bool(service.repository_url),
            "limits": {
                "maxWebhooks": webhooks_service.MAX_TRIGGERS_PER_SERVICE,
                "maxRefsPerDelivery": webhooks_service.MAX_REFS_PER_DELIVERY,
                "maxBuildsPerMinute": webhooks_service.MAX_BUILDS_PER_MINUTE,
            },
        }
    )


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks", methods=["POST"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def create_webhook(service_id: int):
    service = catalog_service.get_service(service_id)
    try:
        data = webhooks_service.create_trigger(service, _payload(), actor=get_current_user())
    except webhooks_service.WebhookError as exc:
        return error_response(str(exc), 400)
    return success_response(data, status_code=201)


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>", methods=["PUT"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def update_webhook(service_id: int, trigger_id: int):
    row = _trigger(service_id, trigger_id)
    try:
        data = webhooks_service.update_trigger(row, _payload(), actor=get_current_user())
    except webhooks_service.WebhookError as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>", methods=["DELETE"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def delete_webhook(service_id: int, trigger_id: int):
    row = _trigger(service_id, trigger_id)
    webhooks_service.delete_trigger(row, actor=get_current_user())
    return success_response({"deleted": True, "id": trigger_id})


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>/deliveries", methods=["GET"])
@require_permission("ci_builds:view")
def list_deliveries(service_id: int, trigger_id: int):
    row = _trigger(service_id, trigger_id)
    limit = request.args.get("limit", type=int) or 25
    items = webhooks_service.list_deliveries(row, limit=limit)
    return success_response({"items": items, "count": len(items)})


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>/secret", methods=["GET"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def reveal_secret(service_id: int, trigger_id: int):
    row = _trigger(service_id, trigger_id)
    try:
        value = webhooks_service.reveal_secret(row, actor=get_current_user())
    except webhooks_service.WebhookError as exc:
        return error_response(str(exc), 404)
    return success_response({"secret": value})


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>/secret", methods=["POST"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def rotate_secret(service_id: int, trigger_id: int):
    row = _trigger(service_id, trigger_id)
    value = webhooks_service.rotate_secret(row, actor=get_current_user())
    return success_response(
        {
            "secret": value,
            "message": "The previous secret stopped working immediately. Update the sender"
            + (" (or press Resync in Bitbucket)." if row.kind == "bitbucket_push" else "."),
        }
    )


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>/preview", methods=["POST"])
@require_permission("ci_builds:view")
def preview_webhook(service_id: int, trigger_id: int):
    """What a body WOULD build — the real planner, nothing queued."""
    row = _trigger(service_id, trigger_id)
    body = _payload()
    sample = body.get("payload")
    if sample is None:
        sample = {}
    if not isinstance(sample, dict):
        return error_response("The sample body must be a JSON object.", 400)
    result = webhooks_service.plan(row, sample, event=str(body.get("event") or ""))
    result["paths"] = webhooks_service.payload_paths(sample) if row.kind == "generic" else []
    return success_response(result)


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>/test", methods=["POST"])
@require_permission("ci_builds:run")
def test_webhook(service_id: int, trigger_id: int):
    """Run a sample body through the real delivery path, as the person asking."""
    row = _trigger(service_id, trigger_id)
    sample = _payload().get("payload")
    if sample is None:
        sample = {}
    if not isinstance(sample, dict):
        return error_response("The sample body must be a JSON object.", 400)
    result = webhooks_service.execute(row, sample, tested_by=get_current_user())
    return success_response(result, status_code=201 if result["triggered"] else 200)


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>/setup", methods=["POST"])
@require_permission("ci_pipelines:edit")
@require_permission("ci_builds:run")
def setup_in_source(service_id: int, trigger_id: int):
    row = _trigger(service_id, trigger_id)
    try:
        data = webhooks_service.configure_in_source(row, actor=get_current_user())
    except webhooks_service.WebhookError as exc:
        return error_response(str(exc), 400)
    return success_response(data)


@ci_webhooks_bp.route("/services/<int:service_id>/webhooks/<int:trigger_id>/source-status", methods=["GET"])
@require_permission("ci_builds:view")
def source_status(service_id: int, trigger_id: int):
    row = _trigger(service_id, trigger_id)
    return success_response(webhooks_service.webhook_status(row))


# ---------------------------------------------------------------------------
# Inbound hook — secret-verified, NOT session authenticated
# ---------------------------------------------------------------------------

def _provided_secret() -> str:
    authorization = request.headers.get("Authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    return (
        request.headers.get("X-KubeSight-Secret")
        or request.headers.get("X-Gitlab-Token")
        or bearer
        or request.args.get("secret", "")
    )


def _delivery_key() -> str:
    for header in webhooks_service.DELIVERY_KEY_HEADERS:
        value = request.headers.get(header, "").strip()
        if value:
            return value
    return ""


@ci_webhooks_bp.route("/hooks/<public_id>", methods=["POST"])
def inbound(public_id: str):
    """Something calling a webhook trigger.

    Status codes are chosen for the two kinds of caller. A source host
    (Bitbucket) reads non-2xx as "retry", so once the secret checks out a push
    webhook always answers 2xx and says in the body what it decided. A script
    or tool calling a generic webhook wants ``curl --fail`` to fail when its
    request was refused, so a generic webhook answers 422 for a request it will
    not honour as sent, and 409 when the build could not be started for a
    reason on this side (service paused, owner lost the right to build).
    """
    if (request.content_length or 0) > webhooks_service.MAX_BODY_BYTES:
        return error_response("Body too large.", 413)
    raw_body = request.get_data(cache=True) or b""
    if len(raw_body) > webhooks_service.MAX_BODY_BYTES:
        return error_response("Body too large.", 413)
    payload = request.get_json(silent=True)
    if payload is None:
        if not raw_body.strip():
            payload = {}  # a bare POST is "build with the defaults"
        elif request.form:
            payload = {key: value for key, value in request.form.items()}
    if not isinstance(payload, dict):
        return error_response("Expected a JSON object body (or no body at all).", 400)

    event = (
        request.headers.get("X-Event-Key")
        or request.headers.get("X-GitHub-Event")
        or request.headers.get("X-Gitlab-Event")
        or ""
    )
    try:
        row, result = webhooks_service.ingest(
            public_id,
            payload,
            event=event,
            provided_secret=_provided_secret(),
            signatures=[
                request.headers.get("X-Hub-Signature-256", ""),
                request.headers.get("X-Hub-Signature", ""),
            ],
            raw_body=raw_body,
            delivery_key=_delivery_key(),
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except PermissionError as exc:
        return error_response(str(exc), 401)

    outcome = result["outcome"]
    if outcome == "triggered":
        return success_response(result, status_code=202)
    if row.kind == "generic" and outcome == "refused":
        return error_response(result["message"], 422, data=result)
    if row.kind == "generic" and outcome == "failed":
        return error_response(result["message"], 409, data=result)
    return success_response(result)
