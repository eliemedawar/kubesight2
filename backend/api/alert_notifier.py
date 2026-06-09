from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

from .db import db
from .email_delivery import EmailDeliveryError, send_alert_email, smtp_is_configured
from .models import AlertNotificationSent, AppSettings
from .notification_routing import serialize_notifications


def _get_notification_settings() -> Dict[str, Any]:
    settings_row = AppSettings.query.first()
    raw = settings_row.notifications if settings_row else {}
    return serialize_notifications(raw)


def dispatch_firing_alert_emails(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
    notifications = _get_notification_settings()
    routing = notifications.get("routing", {})
    email_cfg = routing.get("email", {})

    summary: Dict[str, Any] = {
        "enabled": bool(notifications.get("alerts")) and bool(email_cfg.get("enabled")),
        "recipient": str(email_cfg.get("address", "")).strip(),
        "smtpReady": smtp_is_configured(),
        "sent": 0,
        "skippedAlreadySent": 0,
        "skippedNotFiring": 0,
        "failed": 0,
        "errors": [],
    }

    if not summary["enabled"]:
        summary["message"] = "Email routing is disabled in settings."
        return summary

    if not summary["recipient"]:
        summary["message"] = "Enable email routing and provide a recipient address."
        return summary

    if not summary["smtpReady"]:
        summary["message"] = "Configure SMTP_HOST and SMTP_FROM in the backend .env file."
        return summary

    firing_ids = {alert.get("id") for alert in alerts if alert.get("status") == "firing" and alert.get("id")}

    if firing_ids:
        AlertNotificationSent.query.filter(
            AlertNotificationSent.channel == "email",
            AlertNotificationSent.alert_id.notin_(list(firing_ids)),
        ).delete(synchronize_session=False)
    else:
        AlertNotificationSent.query.filter_by(channel="email").delete(synchronize_session=False)

    for alert in alerts:
        if alert.get("status") != "firing":
            summary["skippedNotFiring"] += 1
            continue

        alert_id = alert.get("id")
        if not alert_id:
            summary["failed"] += 1
            summary["errors"].append("Alert missing id; cannot send email.")
            continue

        already_sent = AlertNotificationSent.query.filter_by(alert_id=alert_id, channel="email").first()
        if already_sent:
            summary["skippedAlreadySent"] += 1
            continue

        try:
            send_alert_email(summary["recipient"], alert)
            db.session.add(
                AlertNotificationSent(
                    alert_id=alert_id,
                    channel="email",
                    sent_at=datetime.now(timezone.utc),
                )
            )
            db.session.commit()
            summary["sent"] += 1
        except EmailDeliveryError as exc:
            db.session.rollback()
            summary["failed"] += 1
            summary["errors"].append(str(exc))
        except Exception as exc:
            db.session.rollback()
            summary["failed"] += 1
            summary["errors"].append(f"Unexpected email error: {exc}")

    if summary["sent"]:
        summary["message"] = f"Sent {summary['sent']} alert email(s) to {summary['recipient']}."
    elif summary["errors"]:
        summary["message"] = summary["errors"][0]
    elif summary["skippedAlreadySent"]:
        summary["message"] = "Alerts already emailed; no new messages sent."
    else:
        summary["message"] = "No firing alerts to email."

    return summary


def _post_json_webhook(url: str, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status >= 400:
            raise EmailDeliveryError(f"Webhook returned HTTP {response.status}")


def _slack_payload(alert: Dict[str, Any]) -> Dict[str, Any]:
    text = (
        f"[{str(alert.get('severity', 'warning')).upper()}] "
        f"{alert.get('title') or 'Alert'} — "
        f"{alert.get('description') or 'Policy alert triggered'}"
    )
    return {"text": text}


def _already_notified(alert_id: str, channel: str) -> bool:
    return (
        AlertNotificationSent.query.filter_by(alert_id=alert_id, channel=channel).first()
        is not None
    )


def _record_notification(alert_id: str, channel: str) -> None:
    db.session.add(
        AlertNotificationSent(
            alert_id=alert_id,
            channel=channel,
            sent_at=datetime.now(timezone.utc),
        )
    )
    db.session.commit()


def dispatch_policy_alert_notifications(
    policy_channels: List[Dict[str, Any]],
    alert: Dict[str, Any],
) -> Dict[str, Any]:
    """Send notifications for a newly fired policy alert based on policy channel config."""
    alert_id = str(alert.get("id") or "").strip()
    if not alert_id:
        return {"sent": 0, "skipped": 0, "errors": ["Alert missing id"]}

    channel_types = {
        str(item.get("channel") if isinstance(item, dict) else item).strip().lower()
        for item in (policy_channels or [])
    }
    channel_types.discard("dashboard")
    if not channel_types:
        return {"sent": 0, "skipped": 0, "errors": []}

    notifications = _get_notification_settings()
    routing = notifications.get("routing", {})
    summary: Dict[str, Any] = {"sent": 0, "skipped": 0, "errors": []}

    if "email" in channel_types:
        email_cfg = routing.get("email", {})
        if (
            notifications.get("alerts")
            and email_cfg.get("enabled")
            and str(email_cfg.get("address", "")).strip()
            and smtp_is_configured()
        ):
            if _already_notified(alert_id, "email"):
                summary["skipped"] += 1
            else:
                try:
                    send_alert_email(str(email_cfg.get("address", "")).strip(), alert)
                    _record_notification(alert_id, "email")
                    summary["sent"] += 1
                except Exception as exc:
                    db.session.rollback()
                    summary["errors"].append(f"email: {exc}")
        else:
            summary["skipped"] += 1

    slack_cfg = routing.get("slack", {})
    if "slack" in channel_types and slack_cfg.get("enabled"):
        webhook_url = str(slack_cfg.get("webhookUrl", "")).strip()
        if webhook_url.startswith("https://hooks.slack.com/"):
            if _already_notified(alert_id, "slack"):
                summary["skipped"] += 1
            else:
                try:
                    _post_json_webhook(webhook_url, _slack_payload(alert))
                    _record_notification(alert_id, "slack")
                    summary["sent"] += 1
                except Exception as exc:
                    db.session.rollback()
                    summary["errors"].append(f"slack: {exc}")
        else:
            summary["skipped"] += 1

    webhook_cfg = routing.get("webhook", {})
    if "webhook" in channel_types and webhook_cfg.get("enabled"):
        webhook_url = str(webhook_cfg.get("url", "")).strip()
        if webhook_url.startswith("https://"):
            if _already_notified(alert_id, "webhook"):
                summary["skipped"] += 1
            else:
                try:
                    _post_json_webhook(webhook_url, alert)
                    _record_notification(alert_id, "webhook")
                    summary["sent"] += 1
                except Exception as exc:
                    db.session.rollback()
                    summary["errors"].append(f"webhook: {exc}")
        else:
            summary["skipped"] += 1

    return summary


def send_test_alert_email() -> Dict[str, Any]:
    notifications = _get_notification_settings()
    routing = notifications.get("routing", {})
    email_cfg = routing.get("email", {})
    recipient = str(email_cfg.get("address", "")).strip()

    if not email_cfg.get("enabled"):
        raise EmailDeliveryError("Enable email in Alert routing before sending a test.")
    if not recipient:
        raise EmailDeliveryError("Configure a recipient email address in Alert routing.")
    if not smtp_is_configured():
        raise EmailDeliveryError("SMTP is not configured on the server.")

    sample_alert = {
        "id": "test-alert",
        "title": "Test alert",
        "severity": "info",
        "status": "firing",
        "clusterId": "test",
        "description": "Manual test from KubeSight",
        "firedAt": datetime.now(timezone.utc).isoformat(),
    }
    send_alert_email(recipient, sample_alert, test=True)
    return {"recipient": recipient, "message": f"Test email sent to {recipient}."}
