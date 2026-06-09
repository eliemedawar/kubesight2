from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

from ..db import db
from ..email_delivery import EmailDeliveryError, send_alert_email, smtp_is_configured
from ..models import (
    AlertDeliveryLog,
    AlertPolicy,
    AlertRoutingDeliverySent,
    AlertRoutingReceiver,
    AlertRoutingReceiverGroup,
    AlertRoutingSmtp,
)
from ..secret_encryption import decrypt_secret, encrypt_secret

VALID_RECEIVER_TYPES = {"email", "webhook", "slack"}
ALLOWED_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
METHODS_WITHOUT_BODY = {"GET", "HEAD", "OPTIONS"}
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


def _receiver_group_names(row: AlertRoutingReceiver) -> List[str]:
    return sorted({group.name for group in row.receiver_groups.all()})


def _group_assigned_policies(row: AlertRoutingReceiverGroup) -> List[Dict[str, Any]]:
    policies = row.policies.order_by(AlertPolicy.name.asc()).all()
    return [{"id": policy.id, "name": policy.name} for policy in policies]


def _serialize_receiver(row: AlertRoutingReceiver) -> Dict[str, Any]:
    assigned = _receiver_assigned_policies(row)
    group_names = _receiver_group_names(row)
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
        "groupNames": group_names,
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
            raise ValueError(
                "HTTP method must be one of GET, POST, PUT, PATCH, DELETE, HEAD, or OPTIONS for webhook receivers."
            )


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


def _parse_email_list(raw: Any) -> List[str]:
    if not raw:
        return []
    emails: List[str] = []
    seen: set[str] = set()
    for line in str(raw).replace(",", "\n").split("\n"):
        address = line.strip()
        if "@" not in address:
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        emails.append(address)
    return emails


def _find_or_create_email_receiver(email: str) -> AlertRoutingReceiver:
    address = email.strip()
    existing = AlertRoutingReceiver.query.filter_by(receiver_type="email", email_address=address).first()
    if existing:
        return existing
    local_part = address.split("@", 1)[0].strip() or "email"
    base_name = f"{local_part.replace('.', ' ').title()} Email"
    name = base_name
    suffix = 2
    while AlertRoutingReceiver.query.filter_by(name=name).first():
        name = f"{base_name} {suffix}"
        suffix += 1
    row = AlertRoutingReceiver(
        name=name,
        receiver_type="email",
        email_address=address,
        enabled=True,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _normalize_receiver_ids(raw: Any) -> List[int]:
    if not isinstance(raw, list):
        return []
    result: List[int] = []
    seen: set[int] = set()
    for item in raw:
        receiver_id = int(item)
        if receiver_id in seen:
            continue
        seen.add(receiver_id)
        result.append(receiver_id)
    return result


def _sync_group_members(group: AlertRoutingReceiverGroup, receiver_ids: List[int]) -> None:
    if not receiver_ids:
        group.members = []
        return
    receivers = (
        AlertRoutingReceiver.query.filter(
            AlertRoutingReceiver.id.in_(receiver_ids),
            AlertRoutingReceiver.enabled.is_(True),
        )
        .order_by(AlertRoutingReceiver.name.asc())
        .all()
    )
    group.members = receivers


def _serialize_receiver_group(row: AlertRoutingReceiverGroup) -> Dict[str, Any]:
    members = list(row.members or [])
    assigned = _group_assigned_policies(row)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "enabled": bool(row.enabled),
        "receiverIds": [member.id for member in members],
        "receiverNames": [member.name for member in members],
        "memberCount": len(members),
        "assignedPolicies": assigned,
        "assignedPolicyNames": [policy["name"] for policy in assigned],
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_receiver_groups() -> List[Dict[str, Any]]:
    rows = AlertRoutingReceiverGroup.query.order_by(AlertRoutingReceiverGroup.name.asc()).all()
    return [_serialize_receiver_group(row) for row in rows]


def _validate_group_payload(
    payload: Dict[str, Any],
    *,
    existing: Optional[AlertRoutingReceiverGroup] = None,
) -> None:
    name = str(payload.get("name", existing.name if existing else "")).strip()
    if not name:
        raise ValueError("Group name is required.")


def create_receiver_group(payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_group_payload(payload)
    name = str(payload.get("name")).strip()
    if AlertRoutingReceiverGroup.query.filter_by(name=name).first():
        raise ValueError("A receiver group with this name already exists.")

    row = AlertRoutingReceiverGroup(
        name=name,
        description=(payload.get("description") or None),
        enabled=bool(payload.get("enabled", True)),
    )
    db.session.add(row)
    db.session.flush()

    receiver_ids = list(_normalize_receiver_ids(payload.get("receiverIds") or []))
    for email in _parse_email_list(payload.get("emailList")):
        receiver_ids.append(_find_or_create_email_receiver(email).id)
    receiver_ids = _normalize_receiver_ids(receiver_ids)
    _sync_group_members(row, receiver_ids)
    db.session.commit()
    return _serialize_receiver_group(row)


def update_receiver_group(group_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = AlertRoutingReceiverGroup.query.get(group_id)
    if not row:
        raise LookupError("Receiver group not found.")
    _validate_group_payload(payload, existing=row)

    if "name" in payload:
        name = str(payload["name"]).strip()
        conflict = AlertRoutingReceiverGroup.query.filter(
            AlertRoutingReceiverGroup.name == name,
            AlertRoutingReceiverGroup.id != row.id,
        ).first()
        if conflict:
            raise ValueError("A receiver group with this name already exists.")
        row.name = name
    if "description" in payload:
        row.description = payload.get("description") or None
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])

    if "receiverIds" in payload or "emailList" in payload:
        if "receiverIds" in payload:
            receiver_ids = _normalize_receiver_ids(payload.get("receiverIds"))
        else:
            receiver_ids = [member.id for member in row.members]
        if "emailList" in payload:
            for email in _parse_email_list(payload.get("emailList")):
                receiver_ids.append(_find_or_create_email_receiver(email).id)
            receiver_ids = _normalize_receiver_ids(receiver_ids)
        _sync_group_members(row, receiver_ids)

    db.session.commit()
    return _serialize_receiver_group(row)


def delete_receiver_group(group_id: int) -> None:
    row = AlertRoutingReceiverGroup.query.get(group_id)
    if not row:
        raise LookupError("Receiver group not found.")
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
            "groupName": row.group_name or "",
            "receiverId": row.receiver_id,
            "receiverName": row.receiver_name,
            "receiverType": row.receiver_type,
            "status": row.status,
            "errorMessage": row.error_message,
            "matchedPattern": row.matched_pattern or "",
            "podName": row.pod_name or "",
            "logSnippet": row.log_snippet or "",
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
    group_name: str = "",
) -> None:
    alert_payload = alert or {}
    db.session.add(
        AlertDeliveryLog(
            alert_id=alert_id,
            alert_name=str(alert_payload.get("title") or alert_payload.get("policyName") or ""),
            policy_id=alert_payload.get("policyId"),
            policy_name=str(alert_payload.get("policyName") or ""),
            group_name=group_name or "",
            receiver_id=receiver.id,
            receiver_name=receiver.name,
            receiver_type=receiver.receiver_type,
            status=status,
            error_message=error_message,
            matched_pattern=str(alert_payload.get("matchedPattern") or "") or None,
            pod_name=str(alert_payload.get("pod") or "") or None,
            log_snippet=str(alert_payload.get("logSnippet") or "") or None,
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


def _webhook_url_with_query(url: str, payload: Dict[str, Any]) -> str:
    flat = {
        str(key): str(value)
        for key, value in payload.items()
        if value is not None and not isinstance(value, (dict, list))
    }
    if not flat:
        return url
    parsed = urlparse(url)
    separator = "&" if parsed.query else "?"
    query = f"{parsed.query}{separator}{urlencode(flat)}" if parsed.query else urlencode(flat)
    return urlunparse(parsed._replace(query=query))


def _post_webhook(receiver: AlertRoutingReceiver, payload: Dict[str, Any]) -> None:
    url = (receiver.url or "").strip()
    if not url:
        raise EmailDeliveryError("Webhook URL is not configured.")
    err = _validate_url(url, slack=(receiver.receiver_type == "slack"))
    if err:
        raise EmailDeliveryError(err)

    headers = _build_webhook_headers(receiver)
    method = "POST" if receiver.receiver_type == "slack" else (receiver.http_method or "POST").upper()

    if method in METHODS_WITHOUT_BODY:
        if method == "GET":
            url = _webhook_url_with_query(url, payload)
        for header in ("Content-Type", "Content-Length"):
            headers.pop(header, None)
        request = urllib.request.Request(url, headers=headers, method=method)
    else:
        body = json.dumps(payload).encode("utf-8")
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


def _destinations_for_policy(policy_id: int) -> List[Tuple[AlertRoutingReceiver, str]]:
    policy = AlertPolicy.query.get(policy_id)
    if not policy:
        return []

    destinations: Dict[int, Tuple[AlertRoutingReceiver, str]] = {}
    for group in list(policy.notification_receiver_groups or []):
        if not group.enabled:
            continue
        for receiver in list(group.members or []):
            if not receiver.enabled:
                continue
            if receiver.id not in destinations:
                destinations[receiver.id] = (receiver, group.name)

    for receiver in list(policy.notification_receivers or []):
        if not receiver.enabled:
            continue
        if receiver.id not in destinations:
            destinations[receiver.id] = (receiver, "")

    return list(destinations.values())


def _slack_alert_payload(alert: Dict[str, Any]) -> Dict[str, Any]:
    if alert.get("alertType") == "log":
        namespace = alert.get("namespace") or "default"
        resource = alert.get("deployment") or alert.get("resourceName") or alert.get("pod") or "workload"
        severity = str(alert.get("severity", "warning")).upper()
        log_lines = alert.get("logLines") or []
        snippet = "\n".join(log_lines[:10]) if log_lines else (alert.get("logSnippet") or "")
        text = (
            f"*{severity}* log alert in `{alert.get('clusterId', '-')}` / `{namespace}`\n"
            f"Resource: `{resource}` | Pod: `{alert.get('pod', '-')}`\n"
            f"Pattern: `{alert.get('matchedPattern', '-')}`\n"
            f"```\n{snippet}\n```"
        )
        return {"text": text}

    namespace = alert.get("namespace") or "default"
    title = alert.get("title") or "Alert"
    text = f"KubeSight Alert: {title} in namespace {namespace}"
    return {"text": text}


def _webhook_log_payload(alert: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "alert_type": "log",
        "policy": alert.get("policyName") or "",
        "severity": alert.get("severity") or "warning",
        "cluster": alert.get("clusterId") or "",
        "namespace": alert.get("namespace") or "",
        "deployment": alert.get("deployment") or "",
        "pod": alert.get("pod") or "",
        "matched_pattern": alert.get("matchedPattern") or "",
        "timestamp": alert.get("detectedAt") or alert.get("firedAt") or "",
        "log_lines": alert.get("logLines") or [],
    }


def _policy_repeat_interval_seconds(policy_id: Optional[int]) -> int:
    from ..alert_policy_catalog import DEFAULT_EVALUATION_INTERVAL_SECONDS

    if not policy_id:
        return DEFAULT_EVALUATION_INTERVAL_SECONDS
    policy = AlertPolicy.query.get(policy_id)
    if not policy:
        return DEFAULT_EVALUATION_INTERVAL_SECONDS
    return int(policy.evaluation_interval_seconds or DEFAULT_EVALUATION_INTERVAL_SECONDS)


def _due_for_repeat_delivery(
    alert_id: str,
    receiver_id: int,
    alert_status: str,
    interval_seconds: int,
) -> bool:
    """Return True when enough time elapsed since the last successful delivery."""
    if alert_status not in {"firing", "active"}:
        return False

    last = (
        AlertDeliveryLog.query.filter_by(
            alert_id=alert_id,
            receiver_id=receiver_id,
            status="success",
        )
        .order_by(AlertDeliveryLog.delivered_at.desc())
        .first()
    )
    if not last or not last.delivered_at:
        return True

    last_at = last.delivered_at
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
    return elapsed >= max(1, int(interval_seconds))


def _mark_delivered(alert_id: str, receiver_id: int, alert_status: str) -> None:
    now = datetime.now(timezone.utc)
    row = AlertRoutingDeliverySent.query.filter_by(
        alert_id=alert_id,
        receiver_id=receiver_id,
        alert_status=alert_status,
    ).first()
    if row:
        row.sent_at = now
        return
    db.session.add(
        AlertRoutingDeliverySent(
            alert_id=alert_id,
            receiver_id=receiver_id,
            alert_status=alert_status,
            sent_at=now,
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

    payload = _webhook_log_payload(alert) if alert.get("alertType") == "log" else alert
    _post_webhook(receiver, payload)


def _dispatch_to_destinations(
    alert: Dict[str, Any],
    destinations: List[Tuple[AlertRoutingReceiver, str]],
    *,
    repeat_interval_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    alert_id = str(alert.get("id") or "").strip()
    alert_status = str(alert.get("status") or "firing").strip().lower()
    if not alert_id:
        return {"sent": 0, "skipped": 0, "errors": ["Alert missing id"]}

    if alert_status not in {"firing", "active"}:
        return {"sent": 0, "skipped": 0, "errors": []}

    interval_seconds = _policy_repeat_interval_seconds(
        int(alert["policyId"]) if alert.get("policyId") else None
    )
    if repeat_interval_seconds is not None:
        interval_seconds = max(1, int(repeat_interval_seconds))

    summary: Dict[str, Any] = {"sent": 0, "skipped": 0, "errors": []}
    for receiver, group_name in destinations:
        if not _due_for_repeat_delivery(alert_id, receiver.id, alert_status, interval_seconds):
            summary["skipped"] += 1
            continue
        try:
            _deliver_to_receiver(receiver, alert)
            _mark_delivered(alert_id, receiver.id, alert_status)
            _record_delivery_log(
                alert_id=alert_id,
                receiver=receiver,
                status="success",
                alert=alert,
                group_name=group_name,
            )
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
                group_name=group_name,
            )
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            label = f"{group_name}: {receiver.name}" if group_name else receiver.name
            summary["errors"].append(f"{label}: {exc}")

    return summary


def dispatch_policy_alert_notifications(alert: Dict[str, Any]) -> Dict[str, Any]:
    policy_id = alert.get("policyId")
    if not policy_id:
        return {"sent": 0, "skipped": 0, "errors": ["Policy alert missing policyId"]}
    interval_seconds = _policy_repeat_interval_seconds(int(policy_id))
    return _dispatch_to_destinations(
        alert,
        _destinations_for_policy(int(policy_id)),
        repeat_interval_seconds=interval_seconds,
    )


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
