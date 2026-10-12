from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

from ..db import db
from ..email_delivery import EmailDeliveryError, SmtpSession, send_alert_email, smtp_is_configured
from ..models import (
    AlertDeliveryLog,
    AlertPolicy,
    AlertRoutingDeliverySent,
    AlertRoutingReceiver,
    AlertRoutingReceiverGroup,
    AlertRoutingSmtp,
    Role,
    User,
)
from ..secret_encryption import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

VALID_RECEIVER_TYPES = {"email", "webhook", "slack", "user", "role"}
# Receiver types whose recipients are email addresses (delivered over SMTP).
EMAIL_CHANNEL_TYPES = {"email", "user", "role"}


def resolve_receiver_emails(receiver: AlertRoutingReceiver) -> List[str]:
    """Effective recipient email addresses for an email-channel receiver.

    user  -> the linked user's email (only if the user is active)
    role  -> emails of all active users holding that role
    email -> the legacy static address
    Disabled/missing users are excluded. Webhook/Slack receivers return [].
    """
    seen: set = set()
    out: List[str] = []

    def _add(addr: Optional[str]) -> None:
        address = (addr or "").strip()
        if "@" in address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)

    if receiver.receiver_type == "user":
        user = receiver.user
        if user and user.is_active:
            _add(user.email)
    elif receiver.receiver_type == "role":
        if receiver.role_id:
            users = User.query.filter(
                User.role_id == receiver.role_id,
                User.is_active.is_(True),
            ).all()
            for user in users:
                _add(user.email)
    elif receiver.receiver_type == "email":
        _add(receiver.email_address)
    return out
ALLOWED_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# ── Receiver filters ────────────────────────────────────────────────────────
# A receiver may narrow which alerts it gets. Semantics (all must match):
#   severity_filter  JSON list of severities (info/warning/critical); the alert's
#                    severity must be in the list. Empty list = every severity.
#   namespace_filter comma-separated list of names or globs (*, ?), e.g.
#                    "payments, prod-*". Empty = every namespace.
#   cluster_filter   same, matched against the alert's cluster id (and the
#                    cluster's display name when it is a registered cluster).
# With a namespace/cluster filter set, an alert that has no namespace/cluster
# (node-level, service or operational alerts) does not match — the filter is
# an allow-list, like Alertmanager matchers on a missing label.
RECEIVER_SEVERITY_LEVELS = ("info", "warning", "critical")
NAMESPACE_FILTER_MAX_LENGTH = 253
CLUSTER_FILTER_MAX_LENGTH = 120


def _filter_patterns(raw: Optional[str]) -> List[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _normalize_severity_filter(raw: Any) -> List[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        raise ValueError("severityFilter must be a list of severities.")
    out: List[str] = []
    for item in items:
        value = str(item or "").strip().lower()
        if not value:
            continue
        if value not in RECEIVER_SEVERITY_LEVELS:
            raise ValueError(
                f"Unknown severity '{value}' in severityFilter (use info, warning, or critical)."
            )
        if value not in out:
            out.append(value)
    # Stable, severity-ordered output.
    return [level for level in RECEIVER_SEVERITY_LEVELS if level in out]


def _normalize_pattern_filter(raw: Any, *, label: str, max_length: int) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(item) for item in raw)
    patterns = _filter_patterns(str(raw))
    value = ", ".join(dict.fromkeys(patterns))
    if len(value) > max_length:
        raise ValueError(f"{label} filter is too long (max {max_length} characters).")
    return value or None


def _matches_any(value: Optional[str], patterns: List[str]) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    return any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns)


def _cluster_display_name(cluster_id: str) -> Optional[str]:
    try:
        from ..cluster_store import get_active_cluster_by_public_id

        cluster = get_active_cluster_by_public_id(cluster_id)
    except Exception:
        return None
    if cluster is None:
        return None
    name = getattr(cluster, "name", None)
    if name is None and isinstance(cluster, dict):
        name = cluster.get("name")
    return str(name) if name else None


def receiver_matches_alert(receiver: AlertRoutingReceiver, alert: Dict[str, Any]) -> bool:
    """True when the alert passes the receiver's severity/namespace/cluster filters."""
    severities = receiver.severity_filter if isinstance(receiver.severity_filter, list) else []
    severities = [str(item).strip().lower() for item in severities if str(item).strip()]
    if severities:
        if str(alert.get("severity") or "").strip().lower() not in severities:
            return False

    namespace_patterns = _filter_patterns(receiver.namespace_filter)
    if namespace_patterns and not _matches_any(alert.get("namespace"), namespace_patterns):
        return False

    cluster_patterns = _filter_patterns(receiver.cluster_filter)
    if cluster_patterns:
        cluster_id = str(alert.get("clusterId") or "").strip()
        if not _matches_any(cluster_id, cluster_patterns):
            names = [alert.get("clusterName")]
            if cluster_id:
                names.append(_cluster_display_name(cluster_id))
            if not any(_matches_any(name, cluster_patterns) for name in names if name):
                return False
    return True
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
    resolved_emails = resolve_receiver_emails(row)
    user = row.user if row.receiver_type == "user" else None
    role = row.role if row.receiver_type == "role" else None
    return {
        "id": row.id,
        "name": row.name,
        "type": row.receiver_type,
        "emailAddress": row.email_address or "",
        "userId": row.user_id,
        "userName": (user.full_name or user.username) if user else None,
        "userActive": bool(user.is_active) if user else None,
        "roleId": row.role_id,
        "roleName": role.name if role else None,
        "recipientEmails": resolved_emails,
        "recipientCount": len(resolved_emails),
        "url": row.url or "",
        "httpMethod": row.http_method or "POST",
        "headers": row.headers if isinstance(row.headers, dict) else {},
        "secretConfigured": bool(row.secret_encrypted),
        "enabled": bool(row.enabled),
        "severityFilter": [
            str(item) for item in (row.severity_filter if isinstance(row.severity_filter, list) else [])
        ],
        "namespaceFilter": row.namespace_filter or "",
        "clusterFilter": row.cluster_filter or "",
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
        raise ValueError("Receiver type must be user, role, email, webhook, or slack.")

    if receiver_type == "user":
        user_id = payload.get("userId", existing.user_id if existing else None)
        if not user_id:
            raise ValueError("Select a user for user receivers.")
        user = User.query.get(int(user_id))
        if not user:
            raise ValueError("Selected user was not found.")
        if not (user.email or "").strip():
            raise ValueError("The selected user has no email address on file.")
    elif receiver_type == "role":
        role_id = payload.get("roleId", existing.role_id if existing else None)
        if not role_id:
            raise ValueError("Select a role for role receivers.")
        if not Role.query.get(int(role_id)):
            raise ValueError("Selected role was not found.")
    elif receiver_type == "email":
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

    # Raises ValueError on bad input; values are applied by create/update.
    if "severityFilter" in payload:
        _normalize_severity_filter(payload.get("severityFilter"))
    if "namespaceFilter" in payload:
        _normalize_pattern_filter(
            payload.get("namespaceFilter"), label="Namespace", max_length=NAMESPACE_FILTER_MAX_LENGTH
        )
    if "clusterFilter" in payload:
        _normalize_pattern_filter(
            payload.get("clusterFilter"), label="Cluster", max_length=CLUSTER_FILTER_MAX_LENGTH
        )


def _apply_receiver_filters(row: AlertRoutingReceiver, payload: Dict[str, Any]) -> None:
    if "severityFilter" in payload:
        row.severity_filter = _normalize_severity_filter(payload.get("severityFilter"))
    elif row.severity_filter is None:
        row.severity_filter = []
    if "namespaceFilter" in payload:
        row.namespace_filter = _normalize_pattern_filter(
            payload.get("namespaceFilter"), label="Namespace", max_length=NAMESPACE_FILTER_MAX_LENGTH
        )
    if "clusterFilter" in payload:
        row.cluster_filter = _normalize_pattern_filter(
            payload.get("clusterFilter"), label="Cluster", max_length=CLUSTER_FILTER_MAX_LENGTH
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
        user_id=int(payload["userId"]) if receiver_type == "user" and payload.get("userId") else None,
        role_id=int(payload["roleId"]) if receiver_type == "role" and payload.get("roleId") else None,
        url=str(payload.get("url", "")).strip() or None,
        http_method=http_method,
        headers=payload.get("headers") if isinstance(payload.get("headers"), dict) else {},
        enabled=bool(payload.get("enabled", True)),
    )
    _apply_receiver_filters(row, payload)
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
    if "userId" in payload:
        row.user_id = int(payload["userId"]) if payload["userId"] else None
    if "roleId" in payload:
        row.role_id = int(payload["roleId"]) if payload["roleId"] else None
    # Keep linkage consistent with the resulting type.
    if row.receiver_type == "user":
        row.role_id = None
    elif row.receiver_type == "role":
        row.user_id = None
    else:
        row.user_id = None
        row.role_id = None
    if "url" in payload:
        row.url = str(payload["url"]).strip() or None
    if "httpMethod" in payload and row.receiver_type == "webhook":
        row.http_method = str(payload["httpMethod"]).strip().upper() or "POST"
    if "headers" in payload and isinstance(payload["headers"], dict):
        row.headers = payload["headers"]
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    _apply_receiver_filters(row, payload)

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
        if receiver.receiver_type in EMAIL_CHANNEL_TYPES:
            if not smtp_is_configured():
                raise EmailDeliveryError("SMTP is not configured.")
            addresses = resolve_receiver_emails(receiver)
            if not addresses:
                raise EmailDeliveryError(
                    "No active recipients for this receiver (user disabled or no email)."
                )
            for address in addresses:
                send_alert_email(address, _webhook_test_payload(), test=True)
            message = f"Test email sent to {', '.join(addresses)}."
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


@dataclass
class ReceiverSnapshot:
    """A detached, thread-safe copy of a receiver for one delivery.

    Deliveries run on worker threads that must not touch ORM objects owned by
    the scheduler's session, so everything a delivery (and the receiver filter)
    needs is copied here on the calling thread. Email recipients are resolved
    on the calling thread too, once per sweep (``emails``).
    """

    id: int
    name: str
    receiver_type: str
    email_address: Optional[str] = None
    user_id: Optional[int] = None
    role_id: Optional[int] = None
    url: Optional[str] = None
    http_method: str = "POST"
    headers: Dict[str, Any] = field(default_factory=dict)
    secret_encrypted: Optional[str] = None
    severity_filter: List[str] = field(default_factory=list)
    namespace_filter: Optional[str] = None
    cluster_filter: Optional[str] = None
    emails: Optional[List[str]] = None


def snapshot_receiver(receiver: Any) -> ReceiverSnapshot:
    if isinstance(receiver, ReceiverSnapshot):
        return receiver
    return ReceiverSnapshot(
        id=receiver.id,
        name=receiver.name or "",
        receiver_type=receiver.receiver_type or "",
        email_address=receiver.email_address,
        user_id=receiver.user_id,
        role_id=receiver.role_id,
        url=receiver.url,
        http_method=receiver.http_method or "POST",
        headers=dict(receiver.headers) if isinstance(receiver.headers, dict) else {},
        secret_encrypted=receiver.secret_encrypted,
        severity_filter=list(receiver.severity_filter)
        if isinstance(receiver.severity_filter, list)
        else [],
        namespace_filter=receiver.namespace_filter,
        cluster_filter=receiver.cluster_filter,
    )


def _snapshot_emails(snapshot: ReceiverSnapshot) -> List[str]:
    """Resolve (once) and cache the recipient emails of an email-channel snapshot."""
    if snapshot.emails is not None:
        return snapshot.emails
    if snapshot.receiver_type not in EMAIL_CHANNEL_TYPES:
        snapshot.emails = []
        return snapshot.emails
    if snapshot.receiver_type == "user":
        user = User.query.get(snapshot.user_id) if snapshot.user_id else None
        candidates = [user.email] if user and user.is_active else []
    elif snapshot.receiver_type == "role":
        candidates = []
        if snapshot.role_id:
            candidates = [
                email
                for (email,) in db.session.query(User.email).filter(
                    User.role_id == snapshot.role_id,
                    User.is_active.is_(True),
                )
            ]
    else:
        candidates = [snapshot.email_address]
    seen: set = set()
    out: List[str] = []
    for addr in candidates:
        address = (addr or "").strip()
        if "@" in address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    snapshot.emails = out
    return out


def destinations_for_policies(
    policy_ids: List[int],
) -> Dict[int, List[Tuple[ReceiverSnapshot, str]]]:
    """Destinations of several policies at once, as detached snapshots.

    One receiver shared by several policies is snapshotted once, so its email
    recipients are resolved once per sweep, not once per alert.
    """
    ids = sorted({int(pid) for pid in policy_ids if pid})
    if not ids:
        return {}
    snapshots: Dict[int, ReceiverSnapshot] = {}

    def _snap(receiver: AlertRoutingReceiver) -> ReceiverSnapshot:
        if receiver.id not in snapshots:
            snapshots[receiver.id] = snapshot_receiver(receiver)
        return snapshots[receiver.id]

    out: Dict[int, List[Tuple[ReceiverSnapshot, str]]] = {}
    for policy in AlertPolicy.query.filter(AlertPolicy.id.in_(ids)).all():
        destinations: Dict[int, Tuple[ReceiverSnapshot, str]] = {}
        for group in list(policy.notification_receiver_groups or []):
            if not group.enabled:
                continue
            for receiver in list(group.members or []):
                if receiver.enabled and receiver.id not in destinations:
                    destinations[receiver.id] = (_snap(receiver), group.name)
        for receiver in list(policy.notification_receivers or []):
            if receiver.enabled and receiver.id not in destinations:
                destinations[receiver.id] = (_snap(receiver), "")
        out[policy.id] = list(destinations.values())
    return out


def _destinations_for_policy(policy_id: int) -> List[Tuple[ReceiverSnapshot, str]]:
    return destinations_for_policies([policy_id]).get(int(policy_id), [])


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

    if alert.get("alertType") == "service":
        severity = str(alert.get("severity", "warning")).upper()
        service_name = alert.get("serviceName") or "Application service"
        detail = alert.get("description") or alert.get("title") or ""
        clients = [str(name) for name in (alert.get("affectedClients") or []) if str(name).strip()]
        text = f"*{severity}* service health alert — {service_name}\n{detail}"
        if clients:
            text += f"\nAffected clients: {', '.join(clients)}"
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


# ── Delivery bookkeeping ────────────────────────────────────────────────────
# ``alert_routing_delivery_sent`` holds, per (alert, receiver):
#   firing | active  -> time of the last successful delivery (normal cadence)
#   retry-first      -> first failed attempt of the current failure streak
#   retry-last       -> latest failed attempt of the current failure streak
# A success clears both retry markers. Markers of alerts that are no longer
# active are pruned by the sweep, so the table stays small.
_SUCCESS_MARKERS = ("firing", "active")
_RETRY_FIRST = "retry-first"
_RETRY_LAST = "retry-last"
_RETRY_MARKERS = (_RETRY_FIRST, _RETRY_LAST)
# First retry after a failed delivery waits this long; each further failure
# doubles the wait, never beyond the policy's repeat interval.
RETRY_BASE_SECONDS = 60
_IN_CHUNK = 500

MarkerMap = Dict[Tuple[str, int], Dict[str, datetime]]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def load_delivery_markers(alert_ids: List[str]) -> MarkerMap:
    """All delivery markers of the given alerts, in one IN query per 500 ids."""
    ids = sorted({str(alert_id) for alert_id in alert_ids if alert_id})
    out: MarkerMap = {}
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        rows = db.session.query(
            AlertRoutingDeliverySent.alert_id,
            AlertRoutingDeliverySent.receiver_id,
            AlertRoutingDeliverySent.alert_status,
            AlertRoutingDeliverySent.sent_at,
        ).filter(AlertRoutingDeliverySent.alert_id.in_(chunk))
        for alert_id, receiver_id, status, sent_at in rows:
            out.setdefault((alert_id, int(receiver_id)), {})[status] = _aware(sent_at)
    return out


def next_delivery_at(markers: Dict[str, datetime], interval_seconds: int) -> Optional[datetime]:
    """When the next delivery attempt is due (None = now).

    Normal cadence: one delivery per repeat interval after the last success.
    After a failure, retry with exponential backoff from ``RETRY_BASE_SECONDS``
    (60 s, 60 s, 120 s, 240 s, ... measured from the start of the failure
    streak), capped at the repeat interval — so a dead receiver is not retried
    on every 15 s tick, and a recovered one is back on its normal cadence after
    its next success.
    """
    interval = max(1, int(interval_seconds))
    successes = [markers[key] for key in _SUCCESS_MARKERS if markers.get(key)]
    last_success = max(successes) if successes else None
    last_failure = markers.get(_RETRY_LAST)
    if last_failure and (last_success is None or last_failure >= last_success):
        first_failure = markers.get(_RETRY_FIRST) or last_failure
        streak = max(0.0, (last_failure - first_failure).total_seconds())
        delay = min(interval, max(RETRY_BASE_SECONDS, streak))
        return last_failure + timedelta(seconds=delay)
    if last_success:
        return last_success + timedelta(seconds=interval)
    return None


def _due_for_repeat_delivery(
    alert_id: str,
    receiver_id: int,
    alert_status: str,
    interval_seconds: int,
    markers: Optional[MarkerMap] = None,
    now: Optional[datetime] = None,
) -> bool:
    """True when a delivery attempt for this alert/receiver is due now."""
    if alert_status not in {"firing", "active"}:
        return False
    if markers is None:
        markers = load_delivery_markers([alert_id])
    due_at = next_delivery_at(markers.get((alert_id, int(receiver_id)), {}), interval_seconds)
    return due_at is None or (now or datetime.now(timezone.utc)) >= due_at


def _upsert_marker(alert_id: str, receiver_id: int, status: str, at: datetime) -> None:
    row = AlertRoutingDeliverySent.query.filter_by(
        alert_id=alert_id, receiver_id=receiver_id, alert_status=status
    ).first()
    if row:
        row.sent_at = at
        return
    db.session.add(
        AlertRoutingDeliverySent(
            alert_id=alert_id, receiver_id=receiver_id, alert_status=status, sent_at=at
        )
    )


def _mark_delivered(alert_id: str, receiver_id: int, alert_status: str) -> None:
    now = datetime.now(timezone.utc)
    _upsert_marker(alert_id, receiver_id, alert_status, now)
    AlertRoutingDeliverySent.query.filter(
        AlertRoutingDeliverySent.alert_id == alert_id,
        AlertRoutingDeliverySent.receiver_id == receiver_id,
        AlertRoutingDeliverySent.alert_status.in_(_RETRY_MARKERS),
    ).delete(synchronize_session=False)


def _mark_failed(alert_id: str, receiver_id: int) -> None:
    now = datetime.now(timezone.utc)
    _upsert_marker(alert_id, receiver_id, _RETRY_LAST, now)
    first = AlertRoutingDeliverySent.query.filter_by(
        alert_id=alert_id, receiver_id=receiver_id, alert_status=_RETRY_FIRST
    ).first()
    if first is None:
        db.session.add(
            AlertRoutingDeliverySent(
                alert_id=alert_id, receiver_id=receiver_id, alert_status=_RETRY_FIRST, sent_at=now
            )
        )


def _deliver_to_receiver(receiver: Any, alert: Dict[str, Any]) -> None:
    """Send one alert to one receiver (an ORM row or a ReceiverSnapshot).

    Email channels: all recipients go over a single SMTP session (one connect
    and login). Fails only when every recipient failed.
    """
    if receiver.receiver_type in EMAIL_CHANNEL_TYPES:
        if not smtp_is_configured():
            raise EmailDeliveryError("SMTP is not configured.")
        if isinstance(receiver, ReceiverSnapshot):
            addresses = _snapshot_emails(receiver)
        else:
            addresses = resolve_receiver_emails(receiver)
        if not addresses:
            raise EmailDeliveryError(
                "No active recipients for this receiver (user disabled or no email)."
            )
        errors: List[str] = []
        with SmtpSession() as smtp:
            for address in addresses:
                try:
                    send_alert_email(address, alert, session=smtp)
                except EmailDeliveryError as exc:
                    errors.append(f"{address}: {exc}")
        if errors and len(errors) == len(addresses):
            raise EmailDeliveryError("; ".join(errors))
        return

    if receiver.receiver_type == "slack":
        _post_webhook(receiver, _slack_alert_payload(alert))
        return

    payload = _webhook_log_payload(alert) if alert.get("alertType") == "log" else alert
    _post_webhook(receiver, payload)


# ── Delivery queue ──────────────────────────────────────────────────────────
# The scheduler tick only decides WHAT is due; the sending (webhooks block up
# to 10 s, SMTP up to 30 s) happens on a small pool, and each worker writes its
# own delivery log row and markers. A dead receiver therefore costs the tick
# nothing, and an (alert, receiver) pair still in flight is never queued twice.
# Tests (app.config["TESTING"]) send inline so results are deterministic.
DELIVERY_WORKERS = max(1, int(os.getenv("ALERT_DELIVERY_WORKERS", "4") or 4))
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_in_flight: set = set()
_in_flight_lock = threading.Lock()
# Tests flip this to exercise the pool path.
_FORCE_ASYNC = False


@dataclass
class _DeliveryJob:
    alert_id: str
    alert_status: str
    alert: Dict[str, Any]
    receiver: ReceiverSnapshot
    group_name: str = ""


def _deliver_inline() -> bool:
    if _FORCE_ASYNC:
        return False
    try:
        from flask import current_app

        return bool(current_app.config.get("TESTING"))
    except Exception:
        return True


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=DELIVERY_WORKERS, thread_name_prefix="alert-delivery"
            )
        return _executor


def _record_attempt(job: _DeliveryJob, error: Optional[str]) -> None:
    """Write the markers and the delivery log row for one attempt."""
    try:
        if error is None:
            _mark_delivered(job.alert_id, job.receiver.id, job.alert_status)
        else:
            _mark_failed(job.alert_id, job.receiver.id)
        _record_delivery_log(
            alert_id=job.alert_id,
            receiver=job.receiver,
            status="success" if error is None else "failed",
            error_message=error,
            alert=job.alert,
            group_name=job.group_name,
        )
        db.session.commit()
        return
    except Exception:
        db.session.rollback()
        logger.exception(
            "Could not record alert delivery markers: alert=%s receiver=%s",
            job.alert_id,
            job.receiver.id,
        )
    try:  # at least keep the log row
        _record_delivery_log(
            alert_id=job.alert_id,
            receiver=job.receiver,
            status="success" if error is None else "failed",
            error_message=error,
            alert=job.alert,
            group_name=job.group_name,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()


def _execute_job(job: _DeliveryJob) -> Optional[str]:
    """Deliver and record one job. Returns the error text, or None on success."""
    try:
        _deliver_to_receiver(job.receiver, job.alert)
        error = None
    except Exception as exc:  # noqa: BLE001 — every failure is logged, never raised
        error = str(exc) or exc.__class__.__name__
    _record_attempt(job, error)
    return error


def _submit_job(job: _DeliveryJob) -> bool:
    """Queue a job on the pool. False when this alert/receiver is in flight."""
    from flask import current_app

    key = (job.alert_id, job.receiver.id)
    with _in_flight_lock:
        if key in _in_flight:
            return False
        _in_flight.add(key)
    app = current_app._get_current_object()

    def _task() -> None:
        try:
            with app.app_context():
                try:
                    _execute_job(job)
                finally:
                    db.session.remove()
        except Exception:
            logger.exception("Alert delivery worker failed: alert=%s", job.alert_id)
        finally:
            with _in_flight_lock:
                _in_flight.discard(key)

    try:
        _get_executor().submit(_task)
    except Exception:
        with _in_flight_lock:
            _in_flight.discard(key)
        raise
    return True


def wait_for_pending_deliveries(timeout: float = 10.0) -> bool:
    """Block until every queued delivery finished (tests, graceful shutdown)."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _in_flight_lock:
            if not _in_flight:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _dispatch_to_destinations(
    alert: Dict[str, Any],
    destinations: List[Tuple[Any, str]],
    *,
    repeat_interval_seconds: Optional[int] = None,
    markers: Optional[MarkerMap] = None,
) -> Dict[str, Any]:
    alert_id = str(alert.get("id") or "").strip()
    alert_status = str(alert.get("status") or "firing").strip().lower()
    if not alert_id:
        return {"sent": 0, "skipped": 0, "errors": ["Alert missing id"]}

    if alert_status not in {"firing", "active"}:
        return {"sent": 0, "skipped": 0, "errors": []}

    if repeat_interval_seconds is not None:
        interval_seconds = max(1, int(repeat_interval_seconds))
    else:
        interval_seconds = _policy_repeat_interval_seconds(
            int(alert["policyId"]) if alert.get("policyId") else None
        )
    if markers is None:
        markers = load_delivery_markers([alert_id])
    inline = _deliver_inline()
    now = datetime.now(timezone.utc)

    summary: Dict[str, Any] = {"sent": 0, "queued": 0, "skipped": 0, "filtered": 0, "errors": []}
    for receiver, group_name in destinations:
        # Receiver filters apply on every path (policy alerts, groups,
        # operational alerts) because they all funnel through here.
        if not receiver_matches_alert(receiver, alert):
            summary["filtered"] += 1
            continue
        if not _due_for_repeat_delivery(
            alert_id, receiver.id, alert_status, interval_seconds, markers=markers, now=now
        ):
            summary["skipped"] += 1
            continue
        snapshot = snapshot_receiver(receiver)
        if snapshot.receiver_type in EMAIL_CHANNEL_TYPES:
            _snapshot_emails(snapshot)  # resolve on this thread, not the worker
        job = _DeliveryJob(
            alert_id=alert_id,
            alert_status=alert_status,
            alert=dict(alert),
            receiver=snapshot,
            group_name=group_name,
        )
        if inline:
            error = _execute_job(job)
            if error is None:
                summary["sent"] += 1
            else:
                label = f"{group_name}: {snapshot.name}" if group_name else snapshot.name
                summary["errors"].append(f"{label}: {error}")
            continue
        if _submit_job(job):
            summary["queued"] += 1
        else:
            summary["skipped"] += 1  # previous attempt still in flight

    return summary


def notify_operational_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Deliver a one-off operational alert to every enabled receiver.

    Unlike policy alerts, this isn't tied to an AlertPolicy — it's used for
    system events like a scheduled change bundle whose image vanished before the
    deploy window. The caller must supply a unique ``id`` (so repeat-delivery
    dedup never suppresses it); ``status`` defaults to ``firing``.
    """
    payload = {"status": "firing", "severity": "warning", **alert}
    receivers = AlertRoutingReceiver.query.filter(
        AlertRoutingReceiver.enabled.is_(True)
    ).all()
    destinations = [(receiver, "") for receiver in receivers]
    if not destinations:
        return {"sent": 0, "skipped": 0, "errors": ["No enabled receivers configured."]}
    # repeat_interval_seconds=1 + a unique alert id => always delivered once.
    return _dispatch_to_destinations(payload, destinations, repeat_interval_seconds=1)


def dispatch_policy_alert_notifications(
    alert: Dict[str, Any],
    *,
    destinations: Optional[List[Tuple[Any, str]]] = None,
    interval_seconds: Optional[int] = None,
    markers: Optional[MarkerMap] = None,
) -> Dict[str, Any]:
    """Deliver a policy alert to its receivers.

    The sweep passes ``destinations``/``interval_seconds``/``markers`` it
    resolved once for all alerts; single-alert callers leave them out.
    """
    policy_id = alert.get("policyId")
    if not policy_id:
        return {"sent": 0, "skipped": 0, "errors": ["Policy alert missing policyId"]}
    if interval_seconds is None:
        interval_seconds = _policy_repeat_interval_seconds(int(policy_id))
    if destinations is None:
        destinations = _destinations_for_policy(int(policy_id))
    return _dispatch_to_destinations(
        alert,
        destinations,
        repeat_interval_seconds=interval_seconds,
        markers=markers,
    )


def prune_delivery_markers(active_alert_ids: List[str]) -> int:
    """Delete delivery markers for alerts that are no longer active.

    Called from the scheduler sweep (never from a read path). With no active
    alerts at all every marker is stale. Reads the (small) set of marker alert
    ids first and only issues a DELETE + commit when something is stale.
    """
    active = {str(alert_id) for alert_id in active_alert_ids if alert_id}
    existing = {
        alert_id
        for (alert_id,) in db.session.query(AlertRoutingDeliverySent.alert_id).distinct()
    }
    stale = sorted(existing - active)
    if not stale:
        return 0
    removed = 0
    for start in range(0, len(stale), _IN_CHUNK):
        chunk = stale[start : start + _IN_CHUNK]
        removed += int(
            AlertRoutingDeliverySent.query.filter(
                AlertRoutingDeliverySent.alert_id.in_(chunk)
            ).delete(synchronize_session=False)
            or 0
        )
    db.session.commit()
    return removed


def delivery_log_retention_days() -> Optional[int]:
    """ALERT_DELIVERY_LOG_RETENTION_DAYS; unset, invalid or <= 0 = keep forever."""
    raw = os.getenv("ALERT_DELIVERY_LOG_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def prune_delivery_logs(*, now: Optional[datetime] = None) -> int:
    """Opt-in retention: delete delivery log rows older than the retention window.

    Off unless ALERT_DELIVERY_LOG_RETENTION_DAYS is set to a positive number of
    days. Returns the number of rows deleted.
    """
    days = delivery_log_retention_days()
    if not days:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    removed = AlertDeliveryLog.query.filter(AlertDeliveryLog.delivered_at < cutoff).delete(
        synchronize_session=False
    )
    db.session.commit()
    return int(removed or 0)
