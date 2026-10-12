from __future__ import annotations

import logging
import time
from typing import Any, Dict

from .email_delivery import smtp_is_configured
from .notification_routing import serialize_notifications
from .db import db
from .models import AppSettings
from .services.alert_routing_service import (
    delivery_log_retention_days,
    destinations_for_policies,
    dispatch_policy_alert_notifications as route_policy_alert_notifications,
    load_delivery_markers,
    prune_delivery_logs,
    prune_delivery_markers,
    send_receiver_test,
    send_smtp_test,
)

logger = logging.getLogger(__name__)


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
    from .alert_policy_catalog import DEFAULT_EVALUATION_INTERVAL_SECONDS
    from .models import AlertHistory, AlertPolicy
    from .services.alert_policy_evaluator import _history_to_alert_dict

    summary: Dict[str, Any] = {"sent": 0, "queued": 0, "skipped": 0, "errors": []}
    rows = AlertHistory.query.filter(
        AlertHistory.status == "active",
        AlertHistory.policy_id.isnot(None),
    ).all()
    active_ids = [f"history-{row.id}" for row in rows]

    notifications = _get_notification_settings()
    if notifications.get("alerts") and rows:
        # Everything the per-alert dispatch needs is resolved once per sweep:
        # policies (one IN query), their destinations (once per policy), and
        # the delivery markers of every active alert (one IN query).
        policy_ids = sorted({row.policy_id for row in rows})
        intervals: Dict[int, int] = {
            policy.id: int(policy.evaluation_interval_seconds or DEFAULT_EVALUATION_INTERVAL_SECONDS)
            for policy in AlertPolicy.query.filter(AlertPolicy.id.in_(policy_ids)).all()
            if policy.enabled
        }
        # Build the alert dicts before any delivery commits expire the rows.
        alerts = [_history_to_alert_dict(row) for row in rows if row.policy_id in intervals]
        if alerts:
            destinations = destinations_for_policies(list(intervals))
            markers = load_delivery_markers([alert["id"] for alert in alerts])
            for alert in alerts:
                policy_id = int(alert["policyId"])
                result = route_policy_alert_notifications(
                    alert,
                    destinations=destinations.get(policy_id, []),
                    interval_seconds=intervals[policy_id],
                    markers=markers,
                )
                summary["sent"] += int(result.get("sent") or 0)
                summary["queued"] += int(result.get("queued") or 0)
                summary["skipped"] += int(result.get("skipped") or 0)
                summary["errors"].extend(result.get("errors") or [])

    prune_delivery_markers(active_ids)
    _maybe_prune_delivery_logs()
    return summary


# Opt-in delivery log retention (ALERT_DELIVERY_LOG_RETENTION_DAYS): when set,
# the sweep deletes old rows at most once an hour; unset = never deleted.
_LOG_RETENTION_EVERY_SECONDS = 3600
_last_log_retention_at = 0.0


def _maybe_prune_delivery_logs() -> None:
    global _last_log_retention_at
    if not delivery_log_retention_days():
        return
    now = time.monotonic()
    if _last_log_retention_at and now - _last_log_retention_at < _LOG_RETENTION_EVERY_SECONDS:
        return
    _last_log_retention_at = now
    try:
        prune_delivery_logs()
    except Exception:
        db.session.rollback()
        logger.exception("Alert delivery log retention failed")


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
