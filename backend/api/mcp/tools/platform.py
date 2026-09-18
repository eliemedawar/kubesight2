"""The machinery underneath: registries, ticketing, mobile releases, and who can do what.

A grab bag by design. These are the surfaces a question lands on a few times a
week rather than a few times an hour, and splitting them into four more domains
would cost an agent a page of routing to save it nothing.

What they have in common is that they are **configuration and provenance**
rather than running state. Where an image came from, which ticket asked for a
deploy, which build became the APK in the store, which role grants the
permission somebody is missing. When an answer in another domain ends in "…and
it was refused", the reason is usually a row in this one.

Two writes live here and both are the same kind of thing — starting an
automation that already exists, and cancelling one. Neither invents a change:
they run a pipeline somebody configured, against a ticket somebody filed. What
is not here is editing that configuration. A registry connection, a ticketing
field mapping or a role is a decision about how the platform behaves, and an
agent proposing one is useful while an agent making one is a surprise.
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import ToolError
from .common import pick, take
from .registry import MAX_ROWS, _limit, tool as _register


def tool(name, **kwargs):
    kwargs.setdefault("domain", "platform")
    return _register(name, **kwargs)


def _user():
    from ...auth_utils import get_current_user

    try:
        return get_current_user()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Images and where they come from
# ---------------------------------------------------------------------------

@tool(
    "kubesight_registries_list",
    permission="registries:view",
    description=(
        "The container registries KubeSight is linked to, with the hosts each "
        "one claims and when it was last reachable. A deploy blocked on a "
        "missing image is answered by checking the registry that owns that host."
    ),
)
def _registries_list(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.registry_service import list_connections

    rows = list_connections() or []
    return {"count": len(rows), "registries": rows}


@tool(
    "kubesight_image_check",
    permission="registries:view",
    description=(
        "Whether an image:tag actually exists in its registry. Run this before "
        "telling somebody a deploy will work — a manifest referencing an image "
        "that was never pushed is the most common reason an apply is rejected, "
        "and it looks like a cluster problem until you check."
    ),
    schema={
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "Full reference, e.g. registry.example.com/app:1.4.2."}
        },
        "required": ["image"],
    },
)
def _image_check(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.registry_service import check_image

    image = str(arguments.get("image") or "").strip()
    if not image:
        raise ToolError("Name the image, tag included.")
    return check_image(image)


# ---------------------------------------------------------------------------
# Tickets and the automation they drive
# ---------------------------------------------------------------------------

@tool(
    "kubesight_ticketing_providers",
    permission="ticketing:view",
    description=(
        "Which ticketing systems are connected (Zoho, Jira), whether each is "
        "enabled and when it last synced. Every other ticketing tool needs one "
        "of the provider keys this returns."
    ),
)
def _ticketing_providers(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.ticketing import describe_all

    rows = describe_all() or []
    return {"count": len(rows), "items": rows}


def _provider_or_error(key: Any):
    from ...services.ticketing import get, keys

    raw = str(key or "").strip().lower()
    provider = get(raw) if raw else None
    if provider is None:
        raise ToolError(
            f"No ticketing provider '{raw or '(none given)'}'. Configured: "
            + (", ".join(keys()) or "none")
        )
    return provider


@tool(
    "kubesight_tickets_list",
    permission="ticketing:view",
    description=(
        "Inbound DevOps request tickets a provider has delivered, newest first: "
        "what was asked for, by whom, and whether KubeSight has acted on it."
    ),
    schema={
        "type": "object",
        "properties": {
            "provider": {"type": "string", "description": "A key from kubesight_ticketing_providers."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["provider"],
    },
)
def _tickets_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    provider = _provider_or_error(arguments.get("provider"))
    rows = provider.sync.list_inbound_tickets(_limit(arguments, 50)) or []
    return {"provider": provider.key, "count": len(rows), "tickets": rows}


_RUN_FIELDS = (
    "id", "status", "provider", "ticketRecordId", "ticketId", "clusterId",
    "namespace", "startedAt", "finishedAt", "error", "stage",
)


@tool(
    "kubesight_automation_runs_list",
    permission="ticketing:view",
    description=(
        "Deploy automation runs — what KubeSight did in response to a ticket, "
        "and how far each run got. The place to look when somebody says 'the "
        "ticket was approved but nothing happened'."
    ),
    schema={
        "type": "object",
        "properties": {
            "provider": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["provider"],
    },
)
def _automation_runs_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.deploy_automation_service import list_runs

    provider = _provider_or_error(arguments.get("provider"))
    rows = list_runs(limit=_limit(arguments, 50), provider=provider.key) or []
    rows = [pick(row, _RUN_FIELDS) for row in rows]
    return {"provider": provider.key, "count": len(rows), "runs": rows}


@tool(
    "kubesight_automation_run_start",
    permission="ticketing:manage",
    description=(
        "Run the deploy automation for one inbound ticket. This deploys what the "
        "ticket asks for, into the cluster the ticket names — it is a deploy, "
        "not a dry run, and it obeys that cluster's approval rules the same way "
        "a manual deploy does. Read the ticket with kubesight_tickets_list first."
    ),
    approval="Runs through the ordinary deploy path, so an approval-gated cluster still gates it.",
    write=True,
    schema={
        "type": "object",
        "properties": {
            "provider": {"type": "string"},
            "ticketRecordId": {"type": "integer", "description": "From kubesight_tickets_list."},
        },
        "required": ["provider", "ticketRecordId"],
    },
)
def _automation_run_start(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.deploy_automation_service import AutomationError, start_run

    _provider_or_error(arguments.get("provider"))
    record_id = arguments.get("ticketRecordId")
    if record_id is None:
        raise ToolError("A ticketRecordId is required — kubesight_tickets_list has them.")
    try:
        data = start_run(int(record_id), user=user, auto=False)
    except (TypeError, ValueError):
        raise ToolError("ticketRecordId must be a number.")
    except AutomationError as exc:
        raise ToolError(str(exc))
    return {"changed": f"started automation for ticket record {record_id}", **(data or {})}


@tool(
    "kubesight_automation_run_cancel",
    permission="ticketing:manage",
    description=(
        "Cancel an automation run that is still going. Anything it already "
        "applied stays applied — this stops the run, it does not undo it."
    ),
    write=True,
    schema={
        "type": "object",
        "properties": {
            "provider": {"type": "string"},
            "runId": {"type": "integer"},
        },
        "required": ["provider", "runId"],
    },
)
def _automation_run_cancel(arguments: Dict[str, Any], *, user=None) -> Dict[str, Any]:
    from ...services.deploy_automation_service import AutomationError, cancel_run

    _provider_or_error(arguments.get("provider"))
    run_id = arguments.get("runId")
    if run_id is None:
        raise ToolError("A runId is required.")
    try:
        data = cancel_run(int(run_id), user=user)
    except AutomationError as exc:
        raise ToolError(str(exc))
    return {"changed": f"cancelled automation run {run_id}", **(data or {})}


# ---------------------------------------------------------------------------
# Mobile releases
# ---------------------------------------------------------------------------

@tool(
    "kubesight_mobile_apps_list",
    permission="mobile_apps:view",
    description=(
        "Mobile applications KubeSight tracks, with the platforms each is "
        "configured for and its latest build."
    ),
)
def _mobile_apps_list(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.mobile_app_service import list_apps

    rows = list_apps() or []
    return {"count": len(rows), "apps": rows}


@tool(
    "kubesight_mobile_builds_list",
    permission="mobile_apps:view",
    description=(
        "One mobile app's binaries, newest first: platform, version, build "
        "number, whether it is signed, and where it came from."
    ),
    schema={
        "type": "object",
        "properties": {
            "appId": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["appId"],
    },
)
def _mobile_builds_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.mobile_app_service import list_builds

    app_id = arguments.get("appId")
    if app_id is None:
        raise ToolError("An appId is required — kubesight_mobile_apps_list has them.")
    rows = list_builds(int(app_id), _limit(arguments, 50)) or []
    return {"appId": int(app_id), "count": len(rows), "builds": rows}


@tool(
    "kubesight_mobile_publishes_list",
    permission="mobile_apps:view",
    description=(
        "Store publishes for a mobile app: which build went to Google Play or "
        "App Store Connect, when, and whether it was accepted."
    ),
    schema={
        "type": "object",
        "properties": {
            "appId": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["appId"],
    },
)
def _mobile_publishes_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services.mobile_app_service import list_publishes

    app_id = arguments.get("appId")
    if app_id is None:
        raise ToolError("An appId is required.")
    rows = list_publishes(app_id=int(app_id)) or []
    rows = take(rows, _limit(arguments, 50))
    return {"appId": int(app_id), "count": len(rows), "items": rows}


# ---------------------------------------------------------------------------
# Who can do what
# ---------------------------------------------------------------------------

_USER_FIELDS = (
    "id", "username", "email", "fullName", "roleName", "status", "isActive",
    "mfaEnabled", "lastLoginAt", "lockedUntil",
)


@tool(
    "kubesight_users_list",
    permission="users:view",
    description=(
        "KubeSight's users, their role and their account state — active, locked, "
        "MFA enrolled. Read-only: unlocking an account or resetting MFA is an "
        "administrator's action, not this server's."
    ),
    schema={
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}},
    },
)
def _users_list(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services import user_service

    payload = user_service.list_users()
    rows = payload.get("items") if isinstance(payload, dict) else payload
    rows = rows or []
    total = len(rows)
    rows = [pick(row, _USER_FIELDS) for row in take(rows, _limit(arguments, MAX_ROWS))]
    return {"totalMatching": total, "count": len(rows), "users": rows}


@tool(
    "kubesight_roles_list",
    permission="roles:view",
    description=(
        "The roles and the permission keys each one grants. This is how to "
        "answer a permission error precisely: name the permission that was "
        "missing and the role that has it, so somebody can grant it."
    ),
)
def _roles_list(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...services import role_service

    payload = role_service.list_roles()
    rows = payload.get("items") if isinstance(payload, dict) else payload
    rows = rows or []
    return {"count": len(rows), "roles": rows}


@tool(
    "kubesight_settings_get",
    permission="settings:view",
    description=(
        "KubeSight's own configuration: the default cluster, refresh interval "
        "and how notifications are routed. Useful mainly for one thing — knowing "
        "which cluster a person means when they say 'the cluster'."
    ),
)
def _settings_get(_arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ...models import AppSettings
    from ...notification_routing import serialize_notifications

    row = AppSettings.query.first()
    if row is None:
        return {
            "theme": "system",
            "refreshIntervalSeconds": 30,
            "defaultCluster": None,
            "notifications": serialize_notifications({}),
        }
    notifications = row.notifications if isinstance(row.notifications, dict) else {}
    return {
        "theme": row.theme or "system",
        "refreshIntervalSeconds": int(row.refresh_interval_seconds or 30),
        "defaultCluster": row.default_cluster or None,
        "notifications": serialize_notifications(notifications),
    }
