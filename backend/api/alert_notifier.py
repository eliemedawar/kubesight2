from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .email_delivery import smtp_is_configured
from .notification_routing import serialize_notifications
from .models import AppSettings
from .services.alert_routing_service import (
    dispatch_policy_alert_notifications as route_policy_alert_notifications,
    prune_delivery_markers,
    send_receiver_test,
    send_smtp_test,
)


def _get_notification_settings() -> Dict[str, Any]:
    settings_row = AppSettings.query.first()
    raw = settings_row.notifications if settings_row else {}
    return serialize_notifications(raw)


def alert_delivery_status() -> Dict[str, Any]:
    """Read-only notification status for the alert list API.

    Delivery itself runs on the background scheduler
    (``dispatch_active_alert_notifications``); reading alerts never sends.
    """
    notifications = _get_notification_settings()
    enabled = bool(notifications.get("alerts"))
    return {
        "enabled": enabled,
        "smtpReady": smtp_is_configured(),
        "deliveredBy": "scheduler",
        "message": (
            "Notifications are delivered by the background alert scheduler."
            if enabled
            else "Alert notifications are disabled in settings."
        ),
    }


def dispatch_active_alert_notifications() -> Dict[str, Any]:
    """Scheduler sweep: (re)deliver notifications for every active policy alert.

    Covers every AlertHistory-backed alert type (metric, log, service,
    automation). Delivery is gated per receiver by the policy's repeat interval
    (see ``_due_for_repeat_delivery``), so running this every tick re-notifies
    at most once per interval — the behaviour the alert list GET used to
    provide as a side effect. Alerts on disabled or deleted policies are not
    delivered. Delivery markers for alerts that are no longer active are pruned.
    """
    from .models import AlertHistory, AlertPolicy
    from .services.alert_policy_evaluator import _history_to_alert_dict

    summary: Dict[str, Any] = {"sent": 0, "skipped": 0, "errors": []}
    rows = AlertHistory.query.filter(
        AlertHistory.status == "active",
        AlertHistory.policy_id.isnot(None),
    ).all()
    active_ids = [f"history-{row.id}" for row in rows]

    notifications = _get_notification_settings()
    if notifications.get("alerts"):
        policies: Dict[int, Any] = {}
        for row in rows:
            if row.policy_id not in policies:
                policies[row.policy_id] = AlertPolicy.query.get(row.policy_id)
            policy = policies[row.policy_id]
            if not policy or not policy.enabled:
                continue
            result = route_policy_alert_notifications(_history_to_alert_dict(row))
            summary["sent"] += int(result.get("sent") or 0)
            summary["skipped"] += int(result.get("skipped") or 0)
            summary["errors"].extend(result.get("errors") or [])

    prune_delivery_markers(active_ids)
    return summary


def dispatch_policy_alert_notifications(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Send outbound notifications for a policy alert to its assigned receivers."""
    notifications = _get_notification_settings()
    if not notifications.get("alerts"):
        return {"sent": 0, "skipped": 0, "errors": []}
    return route_policy_alert_notifications(alert)


def dispatch_pending_policy_notifications(policy_id: int) -> Dict[str, Any]:
    """Deliver any outstanding notifications for active alerts on a policy."""
    from .models import AlertHistory
    from .services.alert_policy_evaluator import _history_to_alert_dict

    notifications = _get_notification_settings()
    if not notifications.get("alerts"):
        return {"sent": 0, "skipped": 0, "errors": []}

    summary: Dict[str, Any] = {"sent": 0, "skipped": 0, "errors": []}
    rows = AlertHistory.query.filter_by(policy_id=int(policy_id), status="active").all()
    for row in rows:
        result = dispatch_policy_alert_notifications(_history_to_alert_dict(row))
        summary["sent"] += int(result.get("sent") or 0)
        summary["skipped"] += int(result.get("skipped") or 0)
        summary["errors"].extend(result.get("errors") or [])
    return summary


def send_test_alert_email(recipient: str | None = None) -> Dict[str, Any]:
    return send_smtp_test(recipient)


def send_test_alert_webhook(receiver_id: int) -> Dict[str, Any]:
    return send_receiver_test(receiver_id)
