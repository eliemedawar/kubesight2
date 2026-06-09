from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..db import db
from ..email_delivery import EmailDeliveryError, send_alert_email, smtp_is_configured
from ..models import (
    AlertDeliveryLog,
    AlertPolicy,
    AlertRoutingDeliverySent,
    AlertRoutingReceiver,
    AlertRoutingSmtp,
)
from ..secret_encryption import decrypt_secret, encrypt_secret

VALID_RECEIVER_TYPES = {"email", "webhook", "slack"}
ALLOWED_HTTP_METHODS = {"POST", "PUT", "PATCH"}
WEBHOOK_TIMEOUT_SECONDS = 10


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _validate_url(url: str, *, slack: bool = False) -> Optional[str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "URL must be a valid HTTP or HTTPS endpoint."
    if slack and not url.strip().startswith("https://hooks.slack.com/"):
        return "Slack webhooks must use https://hooks.slack.com/..."
    return None


def get_or_create_smtp() -> AlertRoutingSmtp:
    row = AlertRoutingSmtp.query.first()
    if not row:
        row = AlertRoutingSmtp()
        db.session.add(row)
        db.session.commit()
    return row


def serialize_smtp(row: Optional[AlertRoutingSmtp] = None) -> Dict[str, Any]:
    row = row or get_or_create_smtp()
    configured = bool(
        row.host.strip()
        and row.from_email.strip()
        and (row.password_encrypted or not row.username.strip())
    )
    if row.username.strip() and not row.password_encrypted:
        configured = False
    return {
        "host": row.host or "",
        "port": int(row.port or 587),
        "username": row.username or "",
        "passwordConfigured": bool(row.password_encrypted),
        "fromEmail": row.from_email or "",
        "fromName": row.from_name or "KubeSight",
        "useTls": bool(row.use_tls),
        "useSsl": bool(row.use_ssl),
        "configured": configured and smtp_is_configured(),
        "lastTestAt": _iso(row.last_test_at),
        "lastTestStatus": row.last_test_status,
        "lastTestMessage": row.last_test_message,
    }


def update_smtp(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_or_create_smtp()
    errors: List[str] = []

    host = str(payload.get("host", row.host or "")).strip()
    port_raw = payload.get("port", row.port or 587)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        errors.append("SMTP port must be an integer.")
        port = row.port or 587

    from_email = str(payload.get("fromEmail", row.from_email or "")).strip()
    if not host:
        errors.append("SMTP host is required.")
    if not from_email or "@" not in from_email:
        errors.append("A valid from email address is required.")
    if port < 1 or port > 65535:
        errors.append("SMTP port must be between 1 and 65535.")

    if errors:
        raise ValueError(" ".join(errors))

    row.host = host
    row.port = port
    row.username = str(payload.get("username", row.username or "")).strip()
    row.from_email = from_email
    row.from_name = str(payload.get("fromName", row.from_name or "KubeSight")).strip() or "KubeSight"
    row.use_tls = bool(payload.get("useTls", row.use_tls))
    row.use_ssl = bool(payload.get("useSsl", row.use_ssl))

    password = payload.get("password")
    if password is not None and str(password).strip():
        row.password_encrypted = encrypt_secret(str(password).strip())

    db.session.add(row)
    db.session.commit()
    return serialize_smtp(row)


def _receiver_assigned_policies(row: AlertRoutingReceiver) -> List[Dict[str, Any]]:
    policies = row.policies.order_by(AlertPolicy.name.asc()).all()
    return [{"id": policy.id, "name": policy.name} for policy in policies]


def _serialize_receiver(row: AlertRoutingReceiver) -> Dict[str, Any]:
    assigned = _receiver_assigned_policies(row)
    return {
        "id": row.id,
        "name": row.name,
        "type": row.receiver_type,
        "emailAddress": row.email_address or "",
        "url": row.url or "",
        "httpMethod": row.http_method or "POST",
        "headers": row.headers if isinstance(row.headers, dict) else {},
        "secretConfigured": bool(row.secret_encrypted),
        "enabled": bool(row.enabled),
        "assignedPolicies": assigned,
        "assignedPolicyNames": [policy["name"] for policy in assigned],
        "lastTestAt": _iso(row.last_test_at),
        "lastTestStatus": row.last_test_status,
        "lastTestMessage": row.last_test_message,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_receivers() -> List[Dict[str, Any]]:
    rows = AlertRoutingReceiver.query.order_by(AlertRoutingReceiver.id.asc()).all()
    return [_serialize_receiver(row) for row in rows]


def _validate_receiver_payload(payload: Dict[str, Any], *, existing: Optional[AlertRoutingReceiver] = None) -> None:
    name = str(payload.get("name", existing.name if existing else "")).strip()
    if not name:
        raise ValueError("Receiver name is required.")

    receiver_type = str(payload.get("type", existing.receiver_type if existing else "")).strip().lower()
    if receiver_type not in VALID_RECEIVER_TYPES:
        raise ValueError("Receiver type must be email, webhook, or slack.")

    if receiver_type == "email":
        email = str(payload.get("emailAddress", existing.email_address if existing else "")).strip()
        if "@" not in email:
            raise ValueError("A valid email address is required for email receivers.")
    else:
        url = str(payload.get("url", existing.url if existing else "")).strip()
        err = _validate_url(url, slack=(receiver_type == "slack"))
        if err:
            raise ValueError(err)

    if receiver_type == "webhook":
        method = str(payload.get("httpMethod", existing.http_method if existing else "POST")).strip().upper()
        if method not in ALLOWED_HTTP_METHODS:
            raise ValueError("HTTP method must be POST, PUT, or PATCH for webhook receivers.")


def create_receiver(payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_receiver_payload(payload)
    receiver_type = str(payload.get("type")).strip().lower()
    http_method = "POST"
    if receiver_type == "webhook":
        http_method = str(payload.get("httpMethod") or "POST").strip().upper()

    row = AlertRoutingReceiver(
        name=str(payload.get("name")).strip(),
        receiver_type=receiver_type,
        email_address=str(payload.get("emailAddress", "")).strip() or None,
        url=str(payload.get("url", "")).strip() or None,
        http_method=http_method,
        headers=payload.get("headers") if isinstance(payload.get("headers"), dict) else {},
        enabled=bool(payload.get("enabled", True)),
    )
    secret = payload.get("secret")
    if secret is not None and str(secret).strip():
        row.secret_encrypted = encrypt_secret(str(secret).strip())
    db.session.add(row)
    db.session.commit()
    return _serialize_receiver(row)


def update_receiver(receiver_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = AlertRoutingReceiver.query.get(receiver_id)
    if not row:
        raise LookupError("Receiver not found.")
    _validate_receiver_payload(payload, existing=row)

    if "name" in payload:
        row.name = str(payload["name"]).strip()
    if "type" in payload:
        row.receiver_type = str(payload["type"]).strip().lower()
    if "emailAddress" in payload:
        row.email_address = str(payload["emailAddress"]).strip() or None
    if "url" in payload:
        row.url = str(payload["url"]).strip() or None
    if "httpMethod" in payload and row.receiver_type == "webhook":
        row.http_method = str(payload["httpMethod"]).strip().upper() or "POST"
    if "headers" in payload and isinstance(payload["headers"], dict):
        row.headers = payload["headers"]
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])

    secret = payload.get("secret")
    if secret is not None and str(secret).strip():
        row.secret_encrypted = encrypt_secret(str(secret).strip())

    db.session.add(row)
    db.session.commit()
    return _serialize_receiver(row)


def delete_receiver(receiver_id: int) -> None:
    row = AlertRoutingReceiver.query.get(receiver_id)
    if not row:
        raise LookupError("Receiver not found.")
    db.session.delete(row)
    db.session.commit()


def list_delivery_logs(*, limit: int = 100) -> List[Dict[str, Any]]:
    rows = (
        AlertDeliveryLog.query.order_by(AlertDeliveryLog.delivered_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [
        {
            "id": row.id,
            "alertId": row.alert_id,
            "alertName": row.alert_name or "",
            "policyId": row.policy_id,
            "policyName": row.policy_name or "",
            "receiverId": row.receiver_id,
            "receiverName": row.receiver_name,
            "receiverType": row.receiver_type,
            "status": row.status,
            "errorMessage": row.error_message,
            "deliveredAt": _iso(row.delivered_at),
        }
        for row in rows
    ]


def _record_delivery_log(
    *,
    alert_id: str,
    receiver: AlertRoutingReceiver,
    status: str,
    error_message: Optional[str] = None,
    alert: Optional[Dict[str, Any]] = None,
) -> None:
    alert_payload = alert or {}
    db.session.add(
        AlertDeliveryLog(
            alert_id=alert_id,
            alert_name=str(alert_payload.get("title") or alert_payload.get("policyName") or ""),
            policy_id=alert_payload.get("policyId"),
            policy_name=str(alert_payload.get("policyName") or ""),
            receiver_id=receiver.id,
            receiver_name=receiver.name,
            receiver_type=receiver.receiver_type,
            status=status,
            error_message=error_message,
        )
    )


def _update_receiver_test(receiver: AlertRoutingReceiver, status: str, message: str) -> None:
    receiver.last_test_at = datetime.now(timezone.utc)
    receiver.last_test_status = status
    receiver.last_test_message = message
    db.session.add(receiver)


def _update_smtp_test(row: AlertRoutingSmtp, status: str, message: str) -> None:
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_status = status
    row.last_test_message = message
    db.session.add(row)


def send_smtp_test(recipient: Optional[str] = None) -> Dict[str, Any]:
    if not smtp_is_configured():
        raise EmailDeliveryError("SMTP is not configured. Save SMTP settings first.")

    smtp_row = get_or_create_smtp()
    to_address = (recipient or smtp_row.from_email or "").strip()
    if "@" not in to_address:
        raise EmailDeliveryError("Provide a valid test recipient email address.")

    sample = {
        "id": "smtp-test",
        "title": "KubeSight SMTP test",
        "severity": "info",
        "status": "firing",
        "clusterId": "test",
        "description": "Manual SMTP test from KubeSight Alert Routing settings.",
        "firedAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        send_alert_email(to_address, sample, test=True)
        _update_smtp_test(smtp_row, "success", f"Test email sent to {to_address}.")
        db.session.commit()
        return {"recipient": to_address, "message": f"Test email sent to {to_address}."}
    except EmailDeliveryError as exc:
        db.session.rollback()
        smtp_row = get_or_create_smtp()
        _update_smtp_test(smtp_row, "failed", str(exc))
        db.session.commit()
        raise


def _slack_test_payload() -> Dict[str, Any]:
    return {"text": "KubeSight Alert: Test notification from Alert Routing settings."}


def _webhook_test_payload() -> Dict[str, Any]:
    return {
        "id": "webhook-test",
        "title": "KubeSight webhook test",
        "severity": "info",
        "status": "firing",
        "clusterId": "test",
        "namespace": "default",
        "description": "Manual webhook test from KubeSight Alert Routing settings.",
        "firedAt": datetime.now(timezone.utc).isoformat(),
        "source": "kubesight_test",
    }


def _build_webhook_headers(receiver: AlertRoutingReceiver) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if isinstance(receiver.headers, dict):
        for key, value in receiver.headers.items():
            if key and value is not None:
                headers[str(key)] = str(value)
    secret = decrypt_secret(receiver.secret_encrypted or "")
    if secret:
        headers.setdefault("Authorization", f"Bearer {secret}")
    return headers


def _post_webhook(receiver: AlertRoutingReceiver, payload: Dict[str, Any]) -> None:
    url = (receiver.url or "").strip()
    if not url:
        raise EmailDeliveryError("Webhook URL is not configured.")
    err = _validate_url(url, slack=(receiver.receiver_type == "slack"))
    if err:
        raise EmailDeliveryError(err)

    body = json.dumps(payload).encode("utf-8")
    headers = _build_webhook_headers(receiver)
    method = "POST" if receiver.receiver_type == "slack" else (receiver.http_method or "POST").upper()
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            if response.status >= 400:
                raise EmailDeliveryError(f"Webhook returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise EmailDeliveryError(f"Webhook returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise EmailDeliveryError(f"Webhook request failed: {exc.reason}") from exc


def send_receiver_test(receiver_id: int) -> Dict[str, Any]:
    receiver = AlertRoutingReceiver.query.get(receiver_id)
    if not receiver:
        raise LookupError("Receiver not found.")

    try:
        if receiver.receiver_type == "email":
            if not smtp_is_configured():
                raise EmailDeliveryError("SMTP is not configured.")
            address = (receiver.email_address or "").strip()
            if "@" not in address:
                raise EmailDeliveryError("Receiver email address is invalid.")
            send_alert_email(address, _webhook_test_payload(), test=True)
            message = f"Test email sent to {address}."
        elif receiver.receiver_type == "slack":
            _post_webhook(receiver, _slack_test_payload())
            message = "Test Slack webhook delivered."
        else:
            _post_webhook(receiver, _webhook_test_payload())
            message = "Test webhook delivered."

        _update_receiver_test(receiver, "success", message)
        db.session.commit()
        return {"message": message}
    except (EmailDeliveryError, Exception) as exc:
        db.session.rollback()
        receiver = AlertRoutingReceiver.query.get(receiver_id)
        if receiver:
            _update_receiver_test(receiver, "failed", str(exc))
            db.session.commit()
        raise EmailDeliveryError(str(exc)) from exc


def _receivers_for_policy(policy_id: int) -> List[AlertRoutingReceiver]:
    policy = AlertPolicy.query.get(policy_id)
    if not policy:
        return []
    return [receiver for receiver in policy.notification_receivers if receiver.enabled]


def _slack_alert_payload(alert: Dict[str, Any]) -> Dict[str, Any]:
    namespace = alert.get("namespace") or "default"
    title = alert.get("title") or "Alert"
    text = f"KubeSight Alert: {title} in namespace {namespace}"
    return {"text": text}


def _already_delivered(alert_id: str, receiver_id: int, alert_status: str) -> bool:
    return (
        AlertRoutingDeliverySent.query.filter_by(
            alert_id=alert_id,
            receiver_id=receiver_id,
            alert_status=alert_status,
        ).first()
        is not None
    )


def _mark_delivered(alert_id: str, receiver_id: int, alert_status: str) -> None:
    db.session.add(
        AlertRoutingDeliverySent(
            alert_id=alert_id,
            receiver_id=receiver_id,
            alert_status=alert_status,
        )
    )


def _deliver_to_receiver(receiver: AlertRoutingReceiver, alert: Dict[str, Any]) -> None:
    if receiver.receiver_type == "email":
        if not smtp_is_configured():
            raise EmailDeliveryError("SMTP is not configured.")
        address = (receiver.email_address or "").strip()
        if "@" not in address:
            raise EmailDeliveryError("Invalid email receiver address.")
        send_alert_email(address, alert)
        return

    if receiver.receiver_type == "slack":
        _post_webhook(receiver, _slack_alert_payload(alert))
        return

    _post_webhook(receiver, alert)


def _dispatch_to_receivers(alert: Dict[str, Any], receivers: List[AlertRoutingReceiver]) -> Dict[str, Any]:
    alert_id = str(alert.get("id") or "").strip()
    alert_status = str(alert.get("status") or "firing").strip().lower()
    if not alert_id:
        return {"sent": 0, "skipped": 0, "errors": ["Alert missing id"]}

    if alert_status not in {"firing", "active"}:
        return {"sent": 0, "skipped": 0, "errors": []}

    summary: Dict[str, Any] = {"sent": 0, "skipped": 0, "errors": []}
    for receiver in receivers:
        if _already_delivered(alert_id, receiver.id, alert_status):
            summary["skipped"] += 1
            continue
        try:
            _deliver_to_receiver(receiver, alert)
            _mark_delivered(alert_id, receiver.id, alert_status)
            _record_delivery_log(alert_id=alert_id, receiver=receiver, status="success", alert=alert)
            db.session.commit()
            summary["sent"] += 1
        except Exception as exc:
            db.session.rollback()
            _record_delivery_log(
                alert_id=alert_id,
                receiver=receiver,
                status="failed",
                error_message=str(exc),
                alert=alert,
            )
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            summary["errors"].append(f"{receiver.name}: {exc}")

    return summary


def dispatch_policy_alert_notifications(alert: Dict[str, Any]) -> Dict[str, Any]:
    policy_id = alert.get("policyId")
    if not policy_id:
        return {"sent": 0, "skipped": 0, "errors": ["Policy alert missing policyId"]}
    return _dispatch_to_receivers(alert, _receivers_for_policy(int(policy_id)))


def dispatch_firing_alerts(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }

    firing_ids = {
        str(alert.get("id"))
        for alert in alerts
        if alert.get("status") in {"firing", "active"} and alert.get("id")
    }

    if firing_ids:
        AlertRoutingDeliverySent.query.filter(
            AlertRoutingDeliverySent.alert_id.notin_(list(firing_ids)),
        ).delete(synchronize_session=False)
        db.session.commit()

    for alert in alerts:
        if alert.get("status") not in {"firing", "active"}:
            continue
        if not alert.get("policyId"):
            continue
        result = dispatch_policy_alert_notifications(alert)
        summary["sent"] += result.get("sent", 0)
        summary["skipped"] += result.get("skipped", 0)
        summary["errors"].extend(result.get("errors") or [])

    summary["failed"] = len(summary["errors"])
    if summary["sent"]:
        summary["message"] = f"Delivered {summary['sent']} notification(s)."
    elif summary["errors"]:
        summary["message"] = summary["errors"][0]
    elif summary["skipped"]:
        summary["message"] = "Notifications already delivered for current alert state."
    else:
        summary["message"] = "No policy alerts with assigned receivers."
    return summary
