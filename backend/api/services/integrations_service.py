"""Provider-neutral view of every outside system KubeSight connects to.

Nine integrations, nine different schemas. Jira and Zoho track both a last sync
and a last test; Jenkins, SMTP, receivers and registries track only a test;
Bitbucket tracks neither; Hermes has no row at all. Half report success as
``"ok"`` and half as ``"success"``. Some are a single config row, some are a
list of rows.

This module is where that ends. Each adapter maps its own storage onto one
descriptor::

    {
      "key", "name", "category", "status", "enabled",
      "lastTestedAt", "lastSuccessfulSyncAt", "message",
      "capabilities": [...], "usedBy": [...], "actions": [...]
    }

so callers — the hub, and anything built on it later — never learn a provider's
name to render it.

Two rules worth keeping:

* ``describe`` must never test. Every underlying ``test_connection`` commits
  ``last_test_*`` columns, so calling one from a GET would rewrite history just
  by looking at it, and would make listing the hub as slow as its slowest
  network round-trip. Describing reads stored state; testing is an explicit
  action.
* Status is one of four customer-facing words. Internally a connection fails a
  dozen ways; an operator scanning a grid needs to know whether it works, not
  how it broke. The specifics travel in ``message``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..access_engine import is_admin, user_has_permission

# ─── The four customer-facing states ───
CONNECTED = "connected"
DEGRADED = "degraded"
DISABLED = "disabled"
NOT_CONFIGURED = "not_configured"

# Providers disagree on how to spell success and failure.
_OK_VALUES = {"ok", "success", "succeeded", "passed"}
_FAIL_VALUES = {"error", "failed", "failure"}


def _outcome(raw: Optional[str]) -> Optional[bool]:
    """True on success, False on failure, None when never run."""
    if not raw:
        return None
    value = str(raw).strip().lower()
    if value in _OK_VALUES:
        return True
    if value in _FAIL_VALUES:
        return False
    return None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def derive_status(*, configured: bool, enabled: bool, last_outcome: Optional[bool]) -> str:
    """The single place the four states are decided.

    Order matters: an integration nobody configured is not "disabled", and one
    switched off is not "degraded" no matter what its last run did — reporting a
    stale failure for something deliberately turned off sends people chasing a
    problem they already resolved by turning it off.
    """
    if not configured:
        return NOT_CONFIGURED
    if not enabled:
        return DISABLED
    if last_outcome is False:
        return DEGRADED
    return CONNECTED


def _actions(*, configured: bool, enabled: bool, can_manage: bool, testable: bool) -> List[str]:
    actions: List[str] = ["configure"] if can_manage else []
    if testable and configured:
        actions.append("test")
    if can_manage and configured:
        actions.append("disable" if enabled else "enable")
    return actions


def _descriptor(
    *,
    key: str,
    name: str,
    category: str,
    configured: bool,
    enabled: bool,
    last_outcome: Optional[bool],
    last_tested_at: Any = None,
    last_sync_at: Any = None,
    message: str = "",
    capabilities: Optional[List[str]] = None,
    used_by: Optional[List[str]] = None,
    can_manage: bool = False,
    testable: bool = True,
) -> Dict[str, Any]:
    status = derive_status(configured=configured, enabled=enabled, last_outcome=last_outcome)
    return {
        "key": key,
        "name": name,
        "category": category,
        "status": status,
        "enabled": bool(enabled),
        "lastTestedAt": _iso(last_tested_at),
        "lastSuccessfulSyncAt": _iso(last_sync_at),
        "message": message or _default_message(status),
        "capabilities": capabilities or [],
        "usedBy": used_by or [],
        "actions": _actions(
            configured=configured, enabled=enabled, can_manage=can_manage, testable=testable
        ),
    }


def _default_message(status: str) -> str:
    return {
        CONNECTED: "Connection healthy",
        DEGRADED: "The last check did not succeed",
        DISABLED: "Switched off — nothing runs",
        NOT_CONFIGURED: "No connection details saved yet",
    }.get(status, "")


# ─── Per-provider adapters ───
#
# Each returns a descriptor, or None when the provider's own module is missing.
# Every adapter is wrapped in a try/except by describe_all: a hub that cannot
# render because one provider's table has not been migrated yet is worse than a
# hub with one card reading "unavailable".


def _ticketing_descriptor(provider_key: str, name: str, can_manage: bool) -> Optional[Dict[str, Any]]:
    from . import ticketing as ticketing_registry

    provider = ticketing_registry.get(provider_key)
    if provider is None:
        return None
    described = provider.describe()
    # describe() is the card summary and omits lastTestAt; the config dict has it.
    try:
        config = provider.sync.get_config_dict() or {}
    except Exception:  # noqa: BLE001 — status must render even if config is broken
        config = {}
    configured = bool(described.get("configured"))
    enabled = bool(described.get("enabled"))
    # Prefer the sync outcome — it is what the integration is *for*; fall back to
    # the connection test when it has never synced.
    outcome = _outcome(described.get("lastSyncStatus"))
    if outcome is None:
        outcome = _outcome(described.get("lastTestStatus"))
    return _descriptor(
        key=provider_key,
        name=name,
        category="Ticketing",
        configured=configured,
        enabled=enabled,
        last_outcome=outcome,
        last_tested_at=config.get("lastTestAt"),
        last_sync_at=described.get("lastSyncAt") if outcome is not False else None,
        message=described.get("lastSyncMessage") or "",
        capabilities=["ticket-sync", "deployment-approval"],
        used_by=["Deployment requests", "Ticketing"],
        can_manage=can_manage,
    )


def _jira(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    return _ticketing_descriptor("jira", "Jira", can_manage)


def _zoho(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    return _ticketing_descriptor("zoho", "Zoho Desk", can_manage)


def _jenkins(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    from .deploy_automation_service import get_jenkins_dict

    row = get_jenkins_dict() or {}
    configured = bool(row.get("baseUrl") and row.get("routerJobPath") and row.get("apiTokenConfigured"))
    return _descriptor(
        key="jenkins",
        name="Jenkins",
        category="CI/CD",
        configured=configured,
        enabled=bool(row.get("enabled")),
        last_outcome=_outcome(row.get("lastTestStatus")),
        last_tested_at=row.get("lastTestAt"),
        message=row.get("lastTestMessage") or "",
        capabilities=["deploy-trigger", "build-status"],
        used_by=["Deployment automation", "Mobile app builds"],
        can_manage=can_manage,
    )


def _smtp(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    from .alert_routing_service import serialize_smtp

    row = serialize_smtp() or {}
    configured = bool(row.get("configured"))
    return _descriptor(
        key="smtp",
        name="SMTP",
        category="Notifications",
        configured=configured,
        # SMTP has no enable switch — it is on whenever it is configured. Saying
        # "enabled" here keeps the contract uniform without inventing a control
        # the backend cannot honour, which is why "disable" is left out of its
        # actions by can_manage/configured below.
        enabled=configured,
        last_outcome=_outcome(row.get("lastTestStatus")),
        last_tested_at=row.get("lastTestAt"),
        message=row.get("lastTestMessage") or "",
        capabilities=["email-delivery"],
        used_by=["Alert notifications", "Deployment request emails"],
        can_manage=can_manage,
    )


def _receivers(receiver_type: str, key: str, name: str, capabilities: List[str], can_manage: bool):
    from .alert_routing_service import list_receivers

    rows = [r for r in (list_receivers() or []) if r.get("type") == receiver_type]
    if not rows:
        return _descriptor(
            key=key,
            name=name,
            category="Notifications",
            configured=False,
            enabled=False,
            last_outcome=None,
            capabilities=capabilities,
            used_by=["Alert notifications"],
            can_manage=can_manage,
        )

    enabled_rows = [r for r in rows if r.get("enabled")]
    failing = [r for r in enabled_rows if _outcome(r.get("lastTestStatus")) is False]
    # Newest test across the set — "when did we last hear anything".
    tested = [r.get("lastTestAt") for r in rows if r.get("lastTestAt")]
    last_tested = max(tested) if tested else None

    if failing:
        message = (
            f"{len(failing)} of {len(enabled_rows)} destinations failed their last test"
            if len(enabled_rows) > 1
            else (failing[0].get("lastTestMessage") or "The last test failed")
        )
        outcome: Optional[bool] = False
    else:
        message = f"{len(enabled_rows)} of {len(rows)} destinations enabled"
        outcome = True if any(_outcome(r.get("lastTestStatus")) for r in rows) else None

    return _descriptor(
        key=key,
        name=name,
        category="Notifications",
        configured=True,
        enabled=bool(enabled_rows),
        last_outcome=outcome,
        last_tested_at=last_tested,
        message=message,
        capabilities=capabilities,
        used_by=["Alert notifications"],
        can_manage=can_manage,
    )


def _slack(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    return _receivers("slack", "slack", "Slack", ["chat-notify"], can_manage)


def _webhooks(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    return _receivers("webhook", "webhooks", "Webhooks", ["webhook-notify"], can_manage)


def _registries(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    from .registry_service import list_connections

    rows = list_connections() or []
    if not rows:
        return _descriptor(
            key="registries",
            name="Container registries",
            category="Artifacts",
            configured=False,
            enabled=False,
            last_outcome=None,
            capabilities=["image-verify"],
            used_by=["Deployment requests", "Deployment automation"],
            can_manage=can_manage,
        )

    enabled_rows = [r for r in rows if r.get("enabled")]
    failing = [r for r in enabled_rows if _outcome(r.get("lastTestStatus")) is False]
    tested = [r.get("lastTestAt") for r in rows if r.get("lastTestAt")]
    last_tested = max(tested) if tested else None

    if failing:
        names = ", ".join(str(r.get("name")) for r in failing[:3])
        message = f"Unreachable: {names}" if names else "A registry failed its last test"
        outcome: Optional[bool] = False
    else:
        message = f"{len(enabled_rows)} of {len(rows)} registries enabled"
        outcome = True if any(_outcome(r.get("lastTestStatus")) for r in rows) else None

    return _descriptor(
        key="registries",
        name="Container registries",
        category="Artifacts",
        configured=True,
        enabled=bool(enabled_rows),
        last_outcome=outcome,
        last_tested_at=last_tested,
        message=message,
        capabilities=["image-verify"],
        used_by=["Deployment requests", "Deployment automation"],
        can_manage=can_manage,
    )


def _bitbucket(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    from .application_intelligence_service import list_credentials

    # Unlike the other list services this one returns {"items": [...], "count"}.
    rows = (list_credentials() or {}).get("items") or []
    usable = [r for r in rows if r.get("enabled") and r.get("secretConfigured")]
    configured = bool(rows)
    # Bitbucket stores no test history at all, so there is nothing to be
    # degraded about: it is configured or it is not. Claiming "Connected" on the
    # strength of a saved secret would be a guess, but it is the honest reading
    # of what we know — and the message says exactly what we checked.
    return _descriptor(
        key="bitbucket",
        name="Bitbucket",
        category="Source control",
        configured=configured,
        enabled=bool(usable),
        last_outcome=None,
        message=(
            f"{len(usable)} of {len(rows)} credential profiles usable"
            if rows
            else "No credential profiles saved yet"
        ),
        capabilities=["repo-read", "pull-request"],
        used_by=["Application Intelligence", "Application builds"],
        can_manage=can_manage,
        testable=False,
    )


def _hermes(user, can_manage: bool) -> Optional[Dict[str, Any]]:
    import os

    from .application_intelligence_hermes import _secret_value  # noqa: PLC2701 — the only accessor

    endpoint = (os.getenv("HERMES_API_URL") or "").strip()
    try:
        token = (_secret_value("HERMES_API_TOKEN") or "").strip()
    except Exception:  # pragma: no cover - env/file access is best effort
        token = ""
    configured = bool(endpoint and token)
    return _descriptor(
        key="hermes",
        name="Hermes",
        category="Intelligence",
        configured=configured,
        enabled=configured,
        last_outcome=None,
        message=(
            f"Configured against {endpoint}"
            if configured
            else "HERMES_API_URL and HERMES_API_TOKEN are not both set"
        ),
        capabilities=["llm-analysis"],
        used_by=["Application Intelligence"],
        can_manage=False,
    )


# ─── Registry ───
#
# view/manage are permission keys; `admin_only` marks the three whose backing
# routes are `@require_admin` and have no granular key of their own.

_ADAPTERS: List[Dict[str, Any]] = [
    {"key": "jira", "name": "Jira", "category": "Ticketing", "fn": _jira,
     "view": "ticketing:view", "manage": "ticketing:manage"},
    {"key": "zoho", "name": "Zoho Desk", "category": "Ticketing", "fn": _zoho,
     "view": "ticketing:view", "manage": "ticketing:manage"},
    {"key": "jenkins", "name": "Jenkins", "category": "CI/CD", "fn": _jenkins,
     "view": "ticketing:view", "manage": "ticketing:manage"},
    {"key": "smtp", "name": "SMTP", "category": "Notifications", "fn": _smtp, "admin_only": True},
    {"key": "slack", "name": "Slack", "category": "Notifications", "fn": _slack, "admin_only": True},
    {"key": "webhooks", "name": "Webhooks", "category": "Notifications", "fn": _webhooks,
     "admin_only": True},
    {"key": "registries", "name": "Container registries", "category": "Artifacts", "fn": _registries,
     "view": "registries:view", "manage": "registries:manage"},
    {"key": "bitbucket", "name": "Bitbucket", "category": "Source control", "fn": _bitbucket,
     "view": "applications:view", "manage": "applications:manage"},
    {"key": "hermes", "name": "Hermes", "category": "Intelligence", "fn": _hermes,
     "view": "applications:view", "manage": "applications:manage"},
]

_BY_KEY = {entry["key"]: entry for entry in _ADAPTERS}


def can_view(user, entry: Dict[str, Any]) -> bool:
    if entry.get("admin_only"):
        return bool(user) and is_admin(user)
    view_key = entry.get("view")
    if not view_key:
        return True
    return bool(user) and user_has_permission(user, view_key)


def can_manage(user, entry: Dict[str, Any]) -> bool:
    if entry.get("admin_only"):
        return bool(user) and is_admin(user)
    manage_key = entry.get("manage")
    if not manage_key:
        return False
    return bool(user) and user_has_permission(user, manage_key)


def _unavailable(key: str, reason: str) -> Dict[str, Any]:
    """A card that says it could not be read, rather than no card at all.

    Keeps the provider's real name and category so a failure still lands in the
    right group and is recognisable — an "Other / Bitbucket" card would read as
    a different integration rather than a broken one.
    """
    entry = _BY_KEY.get(key, {})
    return {
        "key": key,
        "name": entry.get("name") or key.title(),
        "category": entry.get("category") or "Other",
        "status": NOT_CONFIGURED,
        "enabled": False,
        "lastTestedAt": None,
        "lastSuccessfulSyncAt": None,
        "message": f"Status unavailable: {reason}",
        "capabilities": [],
        "usedBy": [],
        "actions": [],
    }


def describe_one(user, key: str) -> Optional[Dict[str, Any]]:
    entry = _BY_KEY.get(key)
    if entry is None or not can_view(user, entry):
        return None
    try:
        descriptor = entry["fn"](user, can_manage(user, entry))
    except Exception as exc:  # noqa: BLE001 — one broken provider must not blank the hub
        return _unavailable(key, str(exc))
    if descriptor is None:
        return None
    # SMTP has no on/off switch of its own; drop the action rather than offer a
    # control that would do nothing.
    if key == "smtp":
        descriptor["actions"] = [a for a in descriptor["actions"] if a not in ("enable", "disable")]
    return descriptor


def describe_all(user) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in _ADAPTERS:
        if not can_view(user, entry):
            continue
        descriptor = describe_one(user, entry["key"])
        if descriptor is not None:
            items.append(descriptor)
    return items


def adapter_entry(key: str) -> Optional[Dict[str, Any]]:
    return _BY_KEY.get(key)


class IntegrationActionError(Exception):
    """An action could not be performed — reported to the caller verbatim."""


# ─── Test ───
#
# Each provider already has a test that persists its own last_test_* columns and
# returns its own shape. These wrappers reduce all of them to {ok, message}, so
# the hub reports a result without knowing whose it is.


def _normalized_test(raw: Dict[str, Any]) -> Dict[str, Any]:
    outcome = _outcome(raw.get("status"))
    return {
        "ok": outcome is not False,
        "message": raw.get("message") or ("Connection succeeded." if outcome else "Test failed."),
    }


def _test_ticketing(provider_key: str) -> Dict[str, Any]:
    from . import ticketing as ticketing_registry

    provider = ticketing_registry.get(provider_key)
    if provider is None:
        raise IntegrationActionError("This provider is not available.")
    return _normalized_test(provider.sync.test_connection())


def _test_jenkins() -> Dict[str, Any]:
    from .deploy_automation_service import test_jenkins

    return _normalized_test(test_jenkins())


def _test_smtp() -> Dict[str, Any]:
    from .alert_routing_service import send_smtp_test

    try:
        return _normalized_test(send_smtp_test())
    except Exception as exc:  # noqa: BLE001 — EmailDeliveryError and friends
        return {"ok": False, "message": str(exc)}


def _test_receivers(receiver_type: str) -> Dict[str, Any]:
    """Test every enabled destination of this type and report the aggregate.

    One card stands for many receivers, so one click has to exercise all of
    them — testing only the first would let a broken second destination keep
    reporting Connected.
    """
    from .alert_routing_service import list_receivers, send_receiver_test

    rows = [
        r
        for r in (list_receivers() or [])
        if r.get("type") == receiver_type and r.get("enabled")
    ]
    if not rows:
        raise IntegrationActionError(f"No enabled {receiver_type} destinations to test.")
    failures: List[str] = []
    for row in rows:
        try:
            result = send_receiver_test(row["id"])
            if _outcome(result.get("status")) is False:
                failures.append(f"{row.get('name')}: {result.get('message') or 'failed'}")
        except Exception as exc:  # noqa: BLE001 — LookupError / EmailDeliveryError
            failures.append(f"{row.get('name')}: {exc}")
    if failures:
        return {"ok": False, "message": "; ".join(failures)}
    return {"ok": True, "message": f"All {len(rows)} destinations responded."}


def _test_registries() -> Dict[str, Any]:
    from .registry_service import list_connections, test_connection

    rows = [r for r in (list_connections() or []) if r.get("enabled")]
    if not rows:
        raise IntegrationActionError("No enabled registries to test.")
    failures: List[str] = []
    for row in rows:
        try:
            result = test_connection(row["id"])
            if _outcome(result.get("status")) is False:
                failures.append(f"{row.get('name')}: {result.get('message') or 'unreachable'}")
        except Exception as exc:  # noqa: BLE001 — LookupError / transport errors
            failures.append(f"{row.get('name')}: {exc}")
    if failures:
        return {"ok": False, "message": "; ".join(failures)}
    return {"ok": True, "message": f"All {len(rows)} registries reachable."}


def _test_hermes(user) -> Dict[str, Any]:
    from .application_intelligence_service import test_hermes_connection

    try:
        result = test_hermes_connection(user) or {}
    except Exception as exc:  # noqa: BLE001 — HermesError / HermesTransientError
        return {"ok": False, "message": str(exc)}
    model = result.get("model")
    latency = result.get("latencyMs")
    detail = f"Reached {model}" if model else "Reached Hermes"
    if latency is not None:
        detail = f"{detail} in {latency}ms"
    return {"ok": bool(result.get("connected", True)), "message": f"{detail}."}


def run_test(user, key: str) -> Dict[str, Any]:
    if key in ("jira", "zoho"):
        return _test_ticketing(key)
    if key == "jenkins":
        return _test_jenkins()
    if key == "smtp":
        return _test_smtp()
    if key in ("slack", "webhooks"):
        return _test_receivers("slack" if key == "slack" else "webhook")
    if key == "registries":
        return _test_registries()
    if key == "hermes":
        return _test_hermes(user)
    raise IntegrationActionError("This integration cannot be tested.")


# ─── Enable / disable ───


def _set_ticketing_enabled(provider_key: str, enabled: bool) -> None:
    from . import ticketing as ticketing_registry

    provider = ticketing_registry.get(provider_key)
    if provider is None:
        raise IntegrationActionError("This provider is not available.")
    provider.sync.update_config({"enabled": enabled})


# Both of these flip `enabled` on the rows directly rather than calling the
# providers' update services. Those services take a whole config payload and
# re-validate every field, so a narrow {"enabled": false} is rejected whenever
# any *other* field is currently incomplete — exactly the situation where an
# operator most wants to switch something off. Turning a record off must not
# require it to be valid first.


def _set_receivers_enabled(receiver_type: str, enabled: bool) -> None:
    """Switching the card switches every destination it stands for."""
    from ..db import db
    from ..models import AlertRoutingReceiver

    rows = AlertRoutingReceiver.query.filter(
        AlertRoutingReceiver.receiver_type == receiver_type
    ).all()
    if not rows:
        raise IntegrationActionError(f"No {receiver_type} destinations to change.")
    for row in rows:
        row.enabled = enabled
    db.session.commit()


def _set_registries_enabled(enabled: bool) -> None:
    from ..db import db
    from ..models import RegistryConnection

    rows = RegistryConnection.query.all()
    if not rows:
        raise IntegrationActionError("No registries to change.")
    for row in rows:
        row.enabled = enabled
    db.session.commit()


def set_enabled(user, key: str, enabled: bool) -> None:
    if key in ("jira", "zoho"):
        _set_ticketing_enabled(key, enabled)
        return
    if key == "jenkins":
        from .deploy_automation_service import update_jenkins

        update_jenkins({"enabled": enabled})
        return
    if key in ("slack", "webhooks"):
        _set_receivers_enabled("slack" if key == "slack" else "webhook", enabled)
        return
    if key == "registries":
        _set_registries_enabled(enabled)
        return
    raise IntegrationActionError("This integration cannot be switched on or off here.")


# ─── Activity ───
#
# Each provider keeps its history in a different table. These readers flatten
# them into one event shape: {id, at, outcome, summary, detail}. `outcome` is
# "ok" | "error" | "info" and only drives the timeline dot.


def _event(entry_id: Any, at: Any, outcome: str, summary: str, detail: str = "") -> Dict[str, Any]:
    return {
        "id": str(entry_id),
        "at": _iso(at),
        "outcome": outcome,
        "summary": summary,
        "detail": detail or "",
    }


def _delivery_activity(receiver_type: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Alert deliveries, optionally narrowed to one receiver type."""
    from .alert_routing_service import list_delivery_logs

    rows = list_delivery_logs(limit=limit * 4 if receiver_type else limit) or []
    if receiver_type:
        rows = [r for r in rows if r.get("receiverType") == receiver_type]
    events = []
    for row in rows[:limit]:
        ok = _outcome(row.get("status"))
        events.append(
            _event(
                row.get("id"),
                row.get("deliveredAt"),
                "ok" if ok else "error",
                f"{row.get('alertName') or 'Alert'} → {row.get('receiverName') or 'receiver'}",
                row.get("errorMessage") or "",
            )
        )
    return events


def _automation_activity(limit: int, provider: Optional[str] = None) -> List[Dict[str, Any]]:
    from .deploy_automation_service import list_runs

    rows = list_runs(limit=limit, provider=provider)
    events = []
    for row in rows or []:
        status = str(row.get("status") or "").lower()
        outcome = "ok" if status in ("succeeded", "success", "completed") else (
            "error" if status in ("failed", "error", "cancelled") else "info"
        )
        target = row.get("deploymentName") or row.get("namespace") or "deployment"
        events.append(
            _event(
                row.get("id"),
                row.get("createdAt"),
                outcome,
                f"Deploy {target} ({status or 'pending'})",
                row.get("error") or row.get("ticketNumber") or "",
            )
        )
    return events


def _inbound_ticket_activity(provider: str, limit: int) -> List[Dict[str, Any]]:
    from ..models import ZohoInboundTicket

    rows = (
        ZohoInboundTicket.query.filter(ZohoInboundTicket.provider == provider)
        .order_by(ZohoInboundTicket.received_at.desc())
        .limit(limit)
        .all()
    )
    events = []
    for row in rows:
        events.append(
            _event(
                row.id,
                row.received_at,
                "error" if row.error else ("ok" if row.resolved else "info"),
                f"Ticket {row.ticket_number or row.ticket_id or '—'} received",
                row.error or row.subject or "",
            )
        )
    return events


def _audit_activity(target_types: List[str], limit: int) -> List[Dict[str, Any]]:
    """Fallback feed for integrations with no history table of their own."""
    from ..models import AuditLog

    rows = (
        AuditLog.query.filter(AuditLog.target_type.in_(target_types))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        _event(row.id, row.created_at, "info", str(row.action or "").replace("_", " "), "")
        for row in rows
    ]


def activity_for(key: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    try:
        if key == "smtp":
            return _delivery_activity(None, limit)
        if key == "slack":
            return _delivery_activity("slack", limit)
        if key == "webhooks":
            return _delivery_activity("webhook", limit)
        if key == "jenkins":
            return _automation_activity(limit)
        if key in ("jira", "zoho"):
            return _inbound_ticket_activity(key, limit)
        if key == "registries":
            return _audit_activity(["registry_connection"], limit)
        if key == "bitbucket":
            return _audit_activity(["bitbucket_credential_profile"], limit)
        if key == "hermes":
            return _audit_activity(["hermes"], limit)
    except Exception:  # noqa: BLE001 — an empty timeline beats a 500 on a side panel
        return []
    return []
