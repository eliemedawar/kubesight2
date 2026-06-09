from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .email_delivery import smtp_is_configured
from .notification_routing import serialize_notifications
from .models import AppSettings
from .services.alert_routing_service import (
    dispatch_firing_alerts,
    dispatch_policy_alert_notifications as route_policy_alert_notifications,
    send_receiver_test,
    send_smtp_test,
)


def _get_notification_settings() -> Dict[str, Any]:
    settings_row = AppSettings.query.first()
    raw = settings_row.notifications if settings_row else {}
    return serialize_notifications(raw)


def dispatch_firing_alert_emails(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deliver notifications for firing policy alerts via assigned receivers."""
    notifications = _get_notification_settings()
    if not notifications.get("alerts"):
        return {
            "enabled": False,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
            "message": "Alert notifications are disabled in settings.",
        }

    summary = dispatch_firing_alerts(alerts)
    summary["enabled"] = True
    summary["smtpReady"] = smtp_is_configured()
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
