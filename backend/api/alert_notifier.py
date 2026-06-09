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


def send_test_alert_email(recipient: str | None = None) -> Dict[str, Any]:
    return send_smtp_test(recipient)


def send_test_alert_webhook(receiver_id: int) -> Dict[str, Any]:
    return send_receiver_test(receiver_id)
