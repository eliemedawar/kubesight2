"""Cluster deployment request workflow.

A user requests a deployment/change in a cluster from the Clusters tab. The
request is persisted, an audit entry is written, and the management team is
notified by email (reusing the alert-routing SMTP relay and email receivers).
The email carries signed approve/decline links so a manager can act straight
from their inbox without a logged-in session; the same actions are also exposed
to admins in-app.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jwt
from flask import has_request_context, request

from ..audit import log_audit
from ..auth_utils import _jwt_secret
from ..db import db
from ..email_delivery import EmailDeliveryError, send_email, smtp_is_configured
from ..models import (
    AlertRoutingReceiver,
    DeploymentRequest,
    DeploymentRequestSetting,
    DeploymentRequestVote,
    User,
)

# Audit action names (kept stable so audit log filters can target them).
ACTION_CREATED = "REQUEST_CREATED"
ACTION_APPROVED = "REQUEST_APPROVED"
ACTION_DECLINED = "REQUEST_DECLINED"

VALID_ACTIONS = {"approve", "decline"}
_TOKEN_TYPE = "deployment_request_action"
_TOKEN_TTL_HOURS = 24


class DeploymentRequestError(RuntimeError):
    """Raised for validation / state errors with an associated HTTP status."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Signed action tokens (HMAC via JWT HS256, 24h expiry)
# ---------------------------------------------------------------------------

def generate_action_token(request_id: int, action: str, voter_email: str = "") -> str:
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unsupported action: {action}")
    now = datetime.now(timezone.utc)
    payload = {
        "typ": _TOKEN_TYPE,
        "drid": int(request_id),
        "act": action,
        "eml": (voter_email or "").strip().lower(),
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_TTL_HOURS),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def verify_action_token(token: str, request_id: int, action: str) -> str:
    """Validate a signed token for the given request id + action.

    Returns the voter email embedded in the token (may be empty for legacy
    single-decider tokens). Raises DeploymentRequestError on any mismatch,
    tampering, or expiry.
    """
    if not token:
        raise DeploymentRequestError("Missing approval token.", 401)
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise DeploymentRequestError("This approval link has expired.", 401) from exc
    except jwt.PyJWTError as exc:
        raise DeploymentRequestError("Invalid approval token.", 401) from exc

    if payload.get("typ") != _TOKEN_TYPE:
        raise DeploymentRequestError("Invalid approval token.", 401)
    if str(payload.get("drid")) != str(request_id):
        raise DeploymentRequestError("Token does not match this request.", 401)
    if payload.get("act") != action:
        raise DeploymentRequestError("Token does not match this action.", 401)
    return str(payload.get("eml") or "")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string into a timezone-aware UTC datetime.

    Accepts a trailing ``Z`` (UTC) and naive strings (assumed UTC). Returns None
    for empty input; raises DeploymentRequestError for unparseable values.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DeploymentRequestError("Invalid date/time for the requested window.", 400) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_timezone(value: Any) -> Optional[str]:
    """Return a valid IANA timezone name, or None."""
    name = str(value or "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def _format_window(req: DeploymentRequest) -> Optional[str]:
    """Human-readable requested window in the requester's local timezone."""
    start = req.requested_window_start
    end = req.requested_window_end
    if not start or not end:
        return None
    tz_name = req.requested_window_timezone
    tz = None
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    if tz is not None:
        start_local = start.astimezone(tz)
        end_local = end.astimezone(tz)
        label = tz_name
    else:
        start_local = start
        end_local = end
        label = "UTC"
    if start_local.date() == end_local.date():
        span = f"{start_local.strftime('%Y-%m-%d %H:%M')}–{end_local.strftime('%H:%M')}"
    else:
        span = f"{start_local.strftime('%Y-%m-%d %H:%M')} – {end_local.strftime('%Y-%m-%d %H:%M')}"
    return f"{span} ({label})"


def _vote_tally(row: DeploymentRequest) -> Tuple[int, int]:
    """Return (approve_count, decline_count) of distinct voters."""
    approvals = sum(1 for v in row.votes if v.decision == "approve")
    declines = sum(1 for v in row.votes if v.decision == "decline")
    return approvals, declines


def serialize_request(row: DeploymentRequest) -> Dict[str, Any]:
    requester = row.requester
    decided_by = row.decided_by
    approvals, declines = _vote_tally(row)
    return {
        "id": row.id,
        "clusterId": row.cluster_id,
        "clusterName": row.cluster_name or row.cluster_id,
        "message": row.message,
        "status": row.status,
        "requesterId": row.requester_id,
        "requesterName": (requester.full_name or requester.username) if requester else "Unknown",
        "requesterUsername": requester.username if requester else None,
        "requiredApprovals": row.required_approvals if row.required_approvals is not None else 1,
        "totalRecipients": row.total_recipients or 0,
        "requestedWindowStart": _iso(row.requested_window_start),
        "requestedWindowEnd": _iso(row.requested_window_end),
        "requestedWindowTimezone": row.requested_window_timezone,
        "requestedWindowLabel": _format_window(row),
        "approvals": approvals,
        "declines": declines,
        "votes": [
            {"email": v.voter_email, "decision": v.decision, "at": _iso(v.created_at)}
            for v in sorted(row.votes, key=lambda v: v.created_at or datetime.min.replace(tzinfo=timezone.utc))
        ],
        "decidedById": row.decided_by_user_id,
        "decidedByName": (decided_by.full_name or decided_by.username) if decided_by else None,
        "decidedAt": _iso(row.decided_at),
        "createdAt": _iso(row.created_at),
    }


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def _public_base_url() -> str:
    configured = (
        os.getenv("DEPLOYMENT_REQUEST_BASE_URL", "")
        or os.getenv("PUBLIC_BASE_URL", "")
        or os.getenv("APP_PUBLIC_URL", "")
    ).strip()
    if configured:
        return configured.rstrip("/")
    if has_request_context():
        return request.host_url.rstrip("/")
    return ""


def _dedupe_emails(addresses: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in addresses:
        address = (raw or "").strip()
        if "@" in address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return out


def _emails_from_env() -> List[str]:
    raw = os.getenv("DEPLOYMENT_REQUEST_RECIPIENTS", "").strip()
    if not raw:
        return []
    parts = raw.replace(",", "\n").replace(";", "\n").split("\n")
    return _dedupe_emails(parts)


def _emails_from_receiver_group() -> List[str]:
    """Members of the dedicated approver group, if one is configured.

    The group name defaults to "Deployment Approvers" and can be overridden with
    DEPLOYMENT_REQUEST_RECEIVER_GROUP. Only enabled email-type members are used.
    """
    from ..models import AlertRoutingReceiverGroup

    group_name = os.getenv("DEPLOYMENT_REQUEST_RECEIVER_GROUP", "Deployment Approvers").strip()
    if not group_name:
        return []
    group = AlertRoutingReceiverGroup.query.filter_by(name=group_name).first()
    if not group or not group.enabled:
        return []
    return _group_member_emails(group)


def _all_enabled_email_receivers() -> List[str]:
    from .alert_routing_service import EMAIL_CHANNEL_TYPES, resolve_receiver_emails

    rows = (
        AlertRoutingReceiver.query.filter(
            AlertRoutingReceiver.receiver_type.in_(EMAIL_CHANNEL_TYPES),
            AlertRoutingReceiver.enabled.is_(True),
        )
        .order_by(AlertRoutingReceiver.id.asc())
        .all()
    )
    emails: List[str] = []
    for row in rows:
        emails.extend(resolve_receiver_emails(row))
    return _dedupe_emails(emails)


def _group_member_emails(group) -> List[str]:
    """Resolved emails of a group's enabled email-channel members.

    user/role members resolve to active users' emails (disabled users excluded).
    """
    from .alert_routing_service import resolve_receiver_emails

    emails: List[str] = []
    for member in (group.members or []):
        if member.enabled:
            emails.extend(resolve_receiver_emails(member))
    return _dedupe_emails(emails)


def _list_available_groups() -> List[Dict[str, Any]]:
    """Receiver groups (with their email members) for the configuration UI."""
    from ..models import AlertRoutingReceiverGroup

    groups = AlertRoutingReceiverGroup.query.order_by(AlertRoutingReceiverGroup.name.asc()).all()
    items: List[Dict[str, Any]] = []
    for group in groups:
        emails = _group_member_emails(group)
        items.append(
            {
                "id": group.id,
                "name": group.name,
                "enabled": bool(group.enabled),
                "emails": emails,
                "memberCount": len(emails),
            }
        )
    return items


def _get_or_create_setting() -> DeploymentRequestSetting:
    row = DeploymentRequestSetting.query.first()
    if not row:
        row = DeploymentRequestSetting(recipients=[], group_ids=[], required_approvals=1)
        db.session.add(row)
        db.session.commit()
    return row


def _configured_recipients() -> List[str]:
    row = DeploymentRequestSetting.query.first()
    if not row or not isinstance(row.recipients, list):
        return []
    return _dedupe_emails([str(item) for item in row.recipients])


def _configured_group_ids() -> List[int]:
    row = DeploymentRequestSetting.query.first()
    if not row or not isinstance(row.group_ids, list):
        return []
    out: List[int] = []
    for item in row.group_ids:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _configured_cluster_approvals() -> Dict[str, int]:
    """Per-cluster approval overrides as a ``{clusterId: int}`` map."""
    row = DeploymentRequestSetting.query.first()
    raw = getattr(row, "cluster_required_approvals", None) if row else None
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return out


def cluster_required_approvals(cluster_id: str) -> int:
    """Required approvals for a cluster: its override, else the global default."""
    overrides = _configured_cluster_approvals()
    key = str(cluster_id or "")
    if key in overrides:
        return overrides[key]
    row = DeploymentRequestSetting.query.first()
    if row and row.required_approvals is not None:
        return max(0, int(row.required_approvals))
    return 1


def _emails_from_configured_groups() -> List[str]:
    from ..models import AlertRoutingReceiverGroup

    group_ids = _configured_group_ids()
    if not group_ids:
        return []
    emails: List[str] = []
    for group in AlertRoutingReceiverGroup.query.filter(
        AlertRoutingReceiverGroup.id.in_(group_ids)
    ).all():
        if group.enabled:
            emails.extend(_group_member_emails(group))
    return _dedupe_emails(emails)


def _resolve_recipients_with_source() -> Tuple[List[str], str]:
    """Return (recipients, source-label) using the resolution order below.

    Admin-configured groups and manual recipients are unioned and take priority;
    everything else is a fallback for when nothing has been configured yet.
    """
    from_groups = _emails_from_configured_groups()
    manual = _configured_recipients()
    if from_groups or manual:
        combined = _dedupe_emails([*from_groups, *manual])
        if from_groups and manual:
            return combined, "configuredGroupsAndEmails"
        return combined, ("configuredGroups" if from_groups else "configured")

    env = _emails_from_env()
    if env:
        return env, "env"
    group = _emails_from_receiver_group()
    if group:
        return group, "receiverGroup"
    receivers = _all_enabled_email_receivers()
    if receivers:
        return receivers, "allReceivers"

    from ..models import AlertRoutingSmtp

    smtp_row = AlertRoutingSmtp.query.first()
    fallback = _dedupe_emails([(smtp_row.from_email if smtp_row else "")])
    return fallback, ("smtpFrom" if fallback else "none")


def _management_recipients() -> List[str]:
    """Resolve who the deployment-request email is sent to.

    Resolution order (first non-empty wins) so admins can configure the audience
    from the UI while sensible fallbacks remain:
      1. Admin-configured recipient list (Clusters → Configure Recipients).
      2. DEPLOYMENT_REQUEST_RECIPIENTS env var (comma/semicolon/newline list).
      3. The "Deployment Approvers" alert-routing receiver group (overridable
         via DEPLOYMENT_REQUEST_RECEIVER_GROUP).
      4. All enabled email-type alert-routing receivers.
      5. The SMTP from-address, so a request is never silently dropped.
    """
    recipients, _ = _resolve_recipients_with_source()
    return recipients


def get_recipient_config() -> Dict[str, Any]:
    """Snapshot of recipient configuration for the admin UI."""
    row = DeploymentRequestSetting.query.first()
    configured = _configured_recipients()
    resolved, source = _resolve_recipients_with_source()
    return {
        "recipients": configured,
        "groupIds": _configured_group_ids(),
        "requiredApprovals": (row.required_approvals if row and row.required_approvals is not None else 1),
        "clusterApprovals": _configured_cluster_approvals(),
        "availableGroups": _list_available_groups(),
        "resolvedRecipients": resolved,
        "poolSize": len(resolved),
        "source": source,
        "smtpConfigured": smtp_is_configured(),
        "updatedAt": _iso(row.updated_at) if row else None,
    }


def update_recipient_config(
    emails: Optional[List[str]] = None,
    group_ids: Optional[List[int]] = None,
    required_approvals: Optional[int] = None,
    cluster_approvals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = _get_or_create_setting()

    if emails is not None:
        if not isinstance(emails, list):
            raise DeploymentRequestError("Recipients must be a list of email addresses.", 400)
        cleaned: List[str] = []
        for raw in emails:
            address = str(raw or "").strip()
            if not address:
                continue
            if "@" not in address or address.startswith("@") or address.endswith("@"):
                raise DeploymentRequestError(f"Invalid email address: {address}", 400)
            cleaned.append(address)
        row.recipients = _dedupe_emails(cleaned)

    if group_ids is not None:
        if not isinstance(group_ids, list):
            raise DeploymentRequestError("Groups must be a list of group IDs.", 400)
        ids: List[int] = []
        for item in group_ids:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                raise DeploymentRequestError(f"Invalid group ID: {item}", 400)
        # Keep only ids that still exist.
        from ..models import AlertRoutingReceiverGroup

        valid = {
            g.id
            for g in AlertRoutingReceiverGroup.query.filter(
                AlertRoutingReceiverGroup.id.in_(ids or [-1])
            ).all()
        }
        row.group_ids = [i for i in ids if i in valid]

    if required_approvals is not None:
        try:
            required = int(required_approvals)
        except (TypeError, ValueError):
            raise DeploymentRequestError("Required approvals must be a whole number.", 400)
        # 0 means "no approval required" — requests are auto-approved on creation.
        row.required_approvals = max(0, required)

    if cluster_approvals is not None:
        if not isinstance(cluster_approvals, dict):
            raise DeploymentRequestError(
                "Cluster approvals must be a map of cluster ID to a whole number.", 400
            )
        overrides: Dict[str, int] = {}
        for key, value in cluster_approvals.items():
            cluster_key = str(key or "").strip()
            if not cluster_key:
                continue
            try:
                count = int(value)
            except (TypeError, ValueError):
                raise DeploymentRequestError(
                    f"Invalid approval count for cluster {cluster_key}.", 400
                )
            if count < 0:
                raise DeploymentRequestError(
                    f"Approval count for cluster {cluster_key} cannot be negative.", 400
                )
            overrides[cluster_key] = count
        row.cluster_required_approvals = overrides

    db.session.add(row)
    db.session.commit()

    # Clamp the quorum to the resolved pool size so it is always satisfiable.
    resolved, _ = _resolve_recipients_with_source()
    pool = len(resolved)
    if pool and row.required_approvals > pool:
        row.required_approvals = pool
        db.session.commit()

    return get_recipient_config()


def _action_urls(req: DeploymentRequest, base_url: str, voter_email: str = "") -> Tuple[str, str]:
    approve_token = generate_action_token(req.id, "approve", voter_email)
    decline_token = generate_action_token(req.id, "decline", voter_email)
    approve_url = f"{base_url}/api/deployment-requests/{req.id}/approve?token={approve_token}"
    decline_url = f"{base_url}/api/deployment-requests/{req.id}/decline?token={decline_token}"
    return approve_url, decline_url


def _html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_email(
    req: DeploymentRequest, requester_name: str, voter_email: str = ""
) -> Tuple[str, str, Optional[str]]:
    cluster_name = req.cluster_name or req.cluster_id
    subject = f"KubeSight Deployment Request - {cluster_name}"
    base_url = _public_base_url()
    quorum_note = (
        f"This request needs {req.required_approvals} of {req.total_recipients} approval(s)."
        if (req.required_approvals or 1) > 1
        else ""
    )

    window_label = _format_window(req)

    # ----- Plain-text part (always present, fallback for text-only clients) -----
    lines = [
        f"Requester: {requester_name}",
        f"Cluster: {cluster_name}",
    ]
    if window_label:
        lines.append(f"Requested window: {window_label}")
    lines.extend(
        [
            "",
            "Message:",
            req.message,
            "",
            "Status: Pending approval",
        ]
    )
    if quorum_note:
        lines.append(quorum_note)
    if base_url:
        approve_url, decline_url = _action_urls(req, base_url, voter_email)
        lines.extend(
            [
                "",
                "Take action (links expire in 24 hours):",
                f"Approve: {approve_url}",
                f"Decline: {decline_url}",
            ]
        )
    else:
        lines.append("")
        lines.append("Approve or decline this request from the KubeSight Deployment Requests page.")
    text_body = "\n".join(lines)

    # ----- HTML part with Approve / Decline buttons -----
    message_html = _html_escape(req.message).replace("\n", "<br />")
    window_row_html = (
        f'<tr><td style="color:#94a3b8;padding:2px 16px 2px 0;">Requested window</td>'
        f'<td style="padding:2px 0;">{_html_escape(window_label)}</td></tr>'
        if window_label
        else ""
    )
    quorum_html = (
        f'<p style="color:#94a3b8;font-size:13px;margin:16px 0 0;">{_html_escape(quorum_note)}</p>'
        if quorum_note
        else ""
    )
    if base_url:
        approve_url, decline_url = _action_urls(req, base_url, voter_email)
        buttons_html = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0;">
          <tr>
            <td style="padding-right:12px;">
              <a href="{_html_escape(approve_url)}"
                 style="display:inline-block;background:#16a34a;color:#ffffff;text-decoration:none;
                        font-weight:600;padding:11px 26px;border-radius:8px;font-size:14px;">
                ✓ Approve
              </a>
            </td>
            <td>
              <a href="{_html_escape(decline_url)}"
                 style="display:inline-block;background:#dc2626;color:#ffffff;text-decoration:none;
                        font-weight:600;padding:11px 26px;border-radius:8px;font-size:14px;">
                ✕ Decline
              </a>
            </td>
          </tr>
        </table>
        <p style="color:#94a3b8;font-size:12px;margin:0;">These action links expire in 24 hours.</p>
        """
    else:
        buttons_html = (
            '<p style="color:#94a3b8;font-size:13px;margin:24px 0 0;">'
            "Approve or decline this request from the KubeSight Deployment Requests page.</p>"
        )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:24px;background:#0f172a;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:560px;margin:0 auto;background:#1e293b;border:1px solid #334155;border-radius:12px;">
    <tr><td style="padding:28px 32px;">
      <div style="color:#38bdf8;font-weight:600;letter-spacing:.05em;text-transform:uppercase;font-size:12px;">KubeSight</div>
      <h1 style="color:#e2e8f0;font-size:18px;margin:6px 0 20px;">Deployment Request</h1>
      <table role="presentation" cellpadding="0" cellspacing="0" style="color:#e2e8f0;font-size:14px;">
        <tr><td style="color:#94a3b8;padding:2px 16px 2px 0;">Requester</td><td style="padding:2px 0;">{_html_escape(requester_name)}</td></tr>
        <tr><td style="color:#94a3b8;padding:2px 16px 2px 0;">Cluster</td><td style="padding:2px 0;">{_html_escape(cluster_name)}</td></tr>
        {window_row_html}
        <tr><td style="color:#94a3b8;padding:2px 16px 2px 0;vertical-align:top;">Status</td><td style="padding:2px 0;color:#fbbf24;">Pending approval</td></tr>
      </table>
      <div style="margin-top:18px;color:#94a3b8;font-size:13px;">Message</div>
      <div style="margin-top:6px;padding:14px 16px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:14px;line-height:1.5;">{message_html}</div>
      {quorum_html}
      {buttons_html}
    </td></tr>
  </table>
</body>
</html>"""

    return subject, text_body, html_body


def _notify_management(req: DeploymentRequest, requester_name: str, recipients: List[str]) -> Dict[str, Any]:
    if not smtp_is_configured():
        return {"sent": 0, "skipped": True, "reason": "SMTP not configured."}

    if not recipients:
        return {"sent": 0, "skipped": True, "reason": "No management email receivers configured."}

    sent = 0
    errors: List[str] = []
    for address in recipients:
        # Per-recipient token so each approver's click is attributed to them
        # (required for quorum counting).
        subject, body, html_body = _build_email(req, requester_name, voter_email=address)
        try:
            send_email(address, subject, body, html_body=html_body)
            sent += 1
        except EmailDeliveryError as exc:
            errors.append(f"{address}: {exc}")
    return {"sent": sent, "recipients": recipients, "errors": errors}


# ---------------------------------------------------------------------------
# Create / list / decide
# ---------------------------------------------------------------------------

def create_request(
    user: Optional[User],
    cluster_id: str,
    cluster_name: str,
    message: str,
    window_start: Any = None,
    window_end: Any = None,
    window_timezone: Any = None,
) -> Dict[str, Any]:
    cluster_id = (cluster_id or "").strip()
    message = (message or "").strip()
    cluster_name = (cluster_name or "").strip() or cluster_id

    if not cluster_id:
        raise DeploymentRequestError("A cluster is required.", 400)
    if not message:
        raise DeploymentRequestError("A request message is required.", 400)
    if len(message) > 5000:
        raise DeploymentRequestError("Request message is too long (max 5000 characters).", 400)

    start_dt = _parse_iso_datetime(window_start)
    end_dt = _parse_iso_datetime(window_end)
    if not start_dt or not end_dt:
        raise DeploymentRequestError(
            "A requested time window (start and end) is required.", 400
        )
    now = datetime.now(timezone.utc)
    if start_dt <= now:
        raise DeploymentRequestError("The window start must be in the future.", 400)
    if end_dt <= start_dt:
        raise DeploymentRequestError("The window end must be after the start.", 400)
    tz_name = _clean_timezone(window_timezone)

    # Snapshot the audience and quorum at creation time. Use the per-cluster
    # override when set so a cluster configured with 0 auto-approves.
    recipients, _ = _resolve_recipients_with_source()
    configured_required = cluster_required_approvals(cluster_id)
    total = len(recipients)
    # 0 = auto-approve (no approval needed). Otherwise clamp the quorum to the
    # available pool so it stays satisfiable.
    if configured_required <= 0:
        required = 0
    else:
        required = min(configured_required, total) if total else 1

    req = DeploymentRequest(
        requester_id=user.id if user else None,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        message=message,
        status="pending",
        required_approvals=required,
        total_recipients=total,
        requested_window_start=start_dt,
        requested_window_end=end_dt,
        requested_window_timezone=tz_name,
    )
    db.session.add(req)
    db.session.commit()

    requester_name = (user.full_name or user.username) if user else "Unknown"
    log_audit(
        ACTION_CREATED,
        actor=user,
        target_type="deployment_request",
        target_id=str(req.id),
        details={
            "clusterId": cluster_id,
            "clusterName": cluster_name,
            "requestId": req.id,
            "requiredApprovals": required,
            "totalRecipients": total,
            "requestedWindow": _format_window(req),
        },
    )

    # No approval required — finalize immediately and skip the approver emails.
    if required == 0:
        _finalize(req, "approved", actor=None)
        payload = serialize_request(req)
        payload["emailResult"] = {"sent": 0, "skipped": True, "reason": "No approval required."}
        return payload

    email_result = _notify_management(req, requester_name, recipients)

    payload = serialize_request(req)
    payload["emailResult"] = email_result
    return payload


def list_requests(*, limit: int = 200) -> List[Dict[str, Any]]:
    # Reflect any window-start expirations immediately, even if the background
    # scheduler has not ticked yet.
    auto_decline_overdue_requests()
    rows = (
        DeploymentRequest.query.order_by(DeploymentRequest.created_at.desc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    return [serialize_request(row) for row in rows]


def list_requests_for_user(user: Optional[User], *, limit: int = 200) -> List[Dict[str, Any]]:
    # Reflect any window-start expirations immediately, even if the background
    # scheduler has not ticked yet.
    auto_decline_overdue_requests()
    if not user:
        return []
    rows = (
        DeploymentRequest.query.filter(DeploymentRequest.requester_id == user.id)
        .order_by(DeploymentRequest.created_at.desc())
        .limit(max(1, min(int(limit), 500)))
        .all()
    )
    return [serialize_request(row) for row in rows]


def get_request_or_error(request_id: int) -> DeploymentRequest:
    req = DeploymentRequest.query.get(request_id)
    if not req:
        raise DeploymentRequestError("Deployment request not found.", 404)
    return req


def _finalize(
    req: DeploymentRequest,
    status: str,
    *,
    actor: Optional[User] = None,
    reason: Optional[str] = None,
) -> None:
    req.status = status
    req.decided_by_user_id = actor.id if actor else None
    req.decided_at = datetime.now(timezone.utc)
    db.session.commit()
    details = {
        "clusterId": req.cluster_id,
        "clusterName": req.cluster_name,
        "requestId": req.id,
        "status": status,
    }
    if reason:
        details["reason"] = reason
        details["auto"] = True
    log_audit(
        ACTION_APPROVED if status == "approved" else ACTION_DECLINED,
        actor=actor,
        target_type="deployment_request",
        target_id=str(req.id),
        details=details,
    )


def _window_start_passed(req: DeploymentRequest, now: Optional[datetime] = None) -> bool:
    """True if the request has a window whose start time is now or in the past."""
    start = req.requested_window_start
    if start is None:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start <= (now or datetime.now(timezone.utc))


def auto_decline_overdue_requests(now: Optional[datetime] = None) -> int:
    """Auto-decline pending requests whose work window has already started.

    A request must be approved before the requested window begins; once the start
    time passes without the required approvals it is automatically declined.
    Returns the number of requests declined. Safe to call repeatedly.
    """
    now = now or datetime.now(timezone.utc)
    pending = (
        DeploymentRequest.query.filter(
            DeploymentRequest.status == "pending",
            DeploymentRequest.requested_window_start.isnot(None),
        ).all()
    )
    declined = 0
    for req in pending:
        if _window_start_passed(req, now):
            _finalize(req, "declined", actor=None, reason="window_start_passed")
            declined += 1
    return declined


def _evaluate_quorum(req: DeploymentRequest) -> Optional[str]:
    """Decide the outcome from current votes, or None if still pending.

    Approved once approvals reach the required count; declined once it becomes
    impossible for the remaining voters to reach that count.
    """
    approvals, declines = _vote_tally(req)
    required = req.required_approvals or 1
    pool = max(req.total_recipients or 0, required)
    if approvals >= required:
        return "approved"
    if declines > (pool - required):
        return "declined"
    return None


def record_vote(
    request_id: int,
    action: str,
    *,
    voter_email: str,
    cluster_id: Optional[str] = None,
    actor: Optional[User] = None,
) -> Dict[str, Any]:
    """Record one approver's vote and re-evaluate quorum.

    Used by both the signed email links and the in-app approve/decline buttons,
    so both paths share the same quorum logic: the request is only approved once
    ``required_approvals`` distinct voters approve, and declined once enough
    voters decline that the quorum can no longer be reached.
    """
    if action not in VALID_ACTIONS:
        raise DeploymentRequestError("Unsupported action.", 400)

    req = get_request_or_error(request_id)
    if cluster_id is not None and req.cluster_id != cluster_id:
        raise DeploymentRequestError("Request does not match this cluster.", 400)
    if req.status != "pending":
        raise DeploymentRequestError(f"This request has already been {req.status}.", 409)

    # A request can no longer be acted on once its work window has started; it is
    # auto-declined instead of being approvable past the start time.
    if _window_start_passed(req):
        _finalize(req, "declined", actor=actor, reason="window_start_passed")
        raise DeploymentRequestError(
            "The requested window has already started; this request was automatically declined.",
            409,
        )

    email = (voter_email or "").strip().lower()
    if not email:
        # No voter identity (legacy token / actor without an email) — treat as a
        # single decisive action.
        _finalize(req, "approved" if action == "approve" else "declined", actor=actor)
        return serialize_request(req)

    decision = "approve" if action == "approve" else "decline"
    existing = DeploymentRequestVote.query.filter_by(
        request_id=req.id, voter_email=email
    ).first()
    if existing:
        existing.decision = decision
    else:
        db.session.add(
            DeploymentRequestVote(request_id=req.id, voter_email=email, decision=decision)
        )
    db.session.commit()
    db.session.refresh(req)

    outcome = _evaluate_quorum(req)
    if outcome:
        _finalize(req, outcome, actor=actor)
    return serialize_request(req)


def decide_request(
    request_id: int,
    action: str,
    *,
    actor: Optional[User] = None,
) -> Dict[str, Any]:
    """In-app approve/decline: record the acting user's vote toward quorum.

    The signed-in manager's email is the voter identity, so a click here counts
    exactly like clicking the link in their notification email (and never
    double-counts if they do both). The request only finalizes once the quorum
    is reached or becomes unreachable.
    """
    if action not in VALID_ACTIONS:
        raise DeploymentRequestError("Unsupported action.", 400)

    actor_email = (getattr(actor, "email", "") or "").strip().lower() if actor else ""
    return record_vote(request_id, action, voter_email=actor_email, actor=actor)


# ---------------------------------------------------------------------------
# Deploy eligibility (gate cluster deploys on an approved request)
# ---------------------------------------------------------------------------

def _user_is_admin(user: Optional[User]) -> bool:
    """Reuse the shared admin convention so admins bypass the approval gate."""
    if user is None:
        return False
    try:
        from ..access_engine import is_admin
    except ImportError:
        return False
    try:
        return bool(is_admin(user))
    except Exception:
        return False


def _active_approval(user: Optional[User], cluster_id: str) -> Optional[DeploymentRequest]:
    """The user's approved, still-valid request for this cluster.

    An approved request grants eligibility immediately (the moment it is
    approved) and stays valid until its requested window ends. We intentionally
    do not require the window *start* to have arrived: a request must be approved
    before the window opens (otherwise it is auto-declined), so once approved the
    requester should be able to deploy right away rather than waiting for the
    planned start. The window *end* still time-boxes the approval, and one
    approval covers multiple deploys until then.

    When several approved requests qualify, the one with the latest window end is
    returned so eligibility persists for as long as possible.
    """
    if not user or not getattr(user, "id", None):
        return None
    key = str(cluster_id or "")
    if not key:
        return None
    now = datetime.now(timezone.utc)
    candidates = (
        DeploymentRequest.query.filter(
            DeploymentRequest.status == "approved",
            DeploymentRequest.requester_id == user.id,
            DeploymentRequest.cluster_id == key,
        )
        .order_by(DeploymentRequest.requested_window_end.desc())
        .all()
    )
    for req in candidates:
        end = req.requested_window_end
        if not end:
            continue
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if now <= end:
            return req
    return None


def deploy_eligibility(user: Optional[User], cluster_id: str) -> Dict[str, Any]:
    """Whether ``user`` may deploy to ``cluster_id`` right now.

    A cluster requiring approvals only lets a deploy proceed when the same user
    has an ``approved`` request for that cluster that is still valid (its
    requested window has not yet ended). Approval takes effect immediately, so a
    non-admin can deploy as soon as their request is approved; one approval
    covers multiple deploys until the window ends.

    Admins are exempt: they may deploy directly without an approved request, so
    approval is reported as not required for them.
    """
    required = cluster_required_approvals(cluster_id)
    if user is not None and _user_is_admin(user):
        return {
            "clusterId": str(cluster_id or ""),
            "requiredApprovals": required,
            "approvalRequired": False,
            "hasActiveApproval": True,
            "eligible": True,
            "activeRequest": None,
        }
    approval_required = required > 0
    active = _active_approval(user, cluster_id) if approval_required else None
    has_active_approval = active is not None
    eligible = (not approval_required) or has_active_approval
    active_request = None
    if active is not None:
        active_request = {
            "id": active.id,
            "windowLabel": _format_window(active),
            "windowStart": _iso(active.requested_window_start),
            "windowEnd": _iso(active.requested_window_end),
        }
    return {
        "clusterId": str(cluster_id or ""),
        "requiredApprovals": required,
        "approvalRequired": approval_required,
        "hasActiveApproval": has_active_approval,
        "eligible": eligible,
        "activeRequest": active_request,
    }


def assert_deploy_allowed(user: Optional[User], cluster_id: str) -> None:
    """Raise DeploymentRequestError(403) if the user cannot deploy to a cluster."""
    info = deploy_eligibility(user, cluster_id)
    if info["approvalRequired"] and not info["hasActiveApproval"]:
        raise DeploymentRequestError(
            "This cluster requires an approved deployment request before deploying. "
            "Request one from the Clusters tab.",
            403,
        )
