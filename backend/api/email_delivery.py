from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Callable, Dict, Optional

from .secret_encryption import decrypt_secret


class EmailDeliveryError(RuntimeError):
    pass


def _smtp_from_db() -> Optional[Dict[str, Any]]:
    try:
        from .models import AlertRoutingSmtp

        row = AlertRoutingSmtp.query.first()
        if not row or not row.host.strip() or not row.from_email.strip():
            return None
        if row.username.strip() and not row.password_encrypted:
            return None
        from_header = row.from_email.strip()
        if row.from_name.strip():
            from_header = f"{row.from_name.strip()} <{row.from_email.strip()}>"
        return {
            "host": row.host.strip(),
            "port": int(row.port or 587),
            "user": row.username.strip(),
            "password": decrypt_secret(row.password_encrypted or ""),
            "from_addr": from_header,
            "use_tls": bool(row.use_tls),
            "use_ssl": bool(row.use_ssl),
        }
    except Exception:
        return None


def _smtp_from_env() -> Dict[str, Any]:
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from_addr": os.getenv("SMTP_FROM", "").strip(),
        "use_tls": os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"},
        "use_ssl": os.getenv("SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "on"},
    }


def smtp_is_configured() -> bool:
    db_settings = _smtp_from_db()
    if db_settings:
        return True
    env = _smtp_from_env()
    return bool(env["host"] and env["from_addr"])


def _smtp_settings() -> Dict[str, Any]:
    return _smtp_from_db() or _smtp_from_env()


# Sentinel cluster id used by service alert policies (Application Services span
# clusters); never show it as a cluster name in notifications.
_SERVICE_ALERT_CLUSTER_ID = "__app_services__"

_SEVERITY_STYLES = {
    "critical": {"label": "Critical", "color": "#b91c1c", "bg": "#fef2f2", "border": "#fecaca"},
    "warning": {"label": "Warning", "color": "#b45309", "bg": "#fffbeb", "border": "#fde68a"},
    "info": {"label": "Info", "color": "#1d4ed8", "bg": "#eff6ff", "border": "#bfdbfe"},
}


def _severity_style(alert: Dict[str, Any]) -> Dict[str, str]:
    severity = str(alert.get("severity") or "").strip().lower()
    return _SEVERITY_STYLES.get(severity, _SEVERITY_STYLES["warning"])


def _format_alert_timestamp(value: Any) -> str:
    """Render an ISO timestamp as '2026-07-03 14:32:23 UTC' (fall back to raw)."""
    if not value:
        return ""
    from datetime import datetime, timezone

    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _alert_cluster_display(alert: Dict[str, Any]) -> str:
    cluster = str(alert.get("clusterId") or "").strip()
    if not cluster or cluster == _SERVICE_ALERT_CLUSTER_ID:
        return ""
    return cluster


def _alert_headline(alert: Dict[str, Any]) -> str:
    alert_type = alert.get("alertType") or "metric"
    if alert_type == "log":
        workload = alert.get("deployment") or alert.get("pod") or alert.get("resourceName")
        return f"Error detected in logs — {workload}" if workload else "Error detected in logs"
    if alert_type == "service":
        service = alert.get("serviceName") or "Application service"
        resource = str(alert.get("resourceName") or "").strip()
        resource_type = str(alert.get("resourceType") or "component").strip()
        if resource:
            return f"{service} — {resource_type} '{resource}' unhealthy"
        return f"{service} — service health alert"
    if alert_type == "automation":
        target = str(alert.get("ticketNumber") or alert.get("resourceName") or "").strip()
        return f"Deploy automation failed — {target}" if target else "Deploy automation failed"
    return alert.get("title") or "KubeSight alert"


def _build_alert_subject(alert: Dict[str, Any]) -> str:
    severity = str(alert.get("severity", "alert")).upper()
    return f"[KubeSight][{severity}] {_alert_headline(alert)}"


def _metric_conditions_summary(alert: Dict[str, Any]) -> str:
    parts = []
    for item in alert.get("triggeredConditions") or []:
        if not isinstance(item, dict) or item.get("matched") is False:
            continue
        label = item.get("metricLabel") or item.get("metricKey") or "metric"
        operator = str(item.get("operator") or "").strip()
        threshold = item.get("threshold")
        observed = item.get("observedValue")
        text = f"{label} {operator} {threshold}".strip()
        if observed is not None:
            text += f" (observed {observed})"
        parts.append(text)
    return "; ".join(parts)


def _alert_detail_rows(alert: Dict[str, Any]) -> list:
    """Ordered (label, value) pairs for the email, skipping empty fields."""
    alert_type = alert.get("alertType") or "metric"
    rows: list = []

    def add(label: str, value: Any) -> None:
        text = str(value).strip() if value is not None else ""
        if text:
            rows.append((label, text))

    if alert_type == "service":
        add("Service", alert.get("serviceName"))
        clients = [str(name) for name in (alert.get("affectedClients") or []) if str(name).strip()]
        add("Affected clients", ", ".join(clients) if clients else "None linked")
        resource_type = str(alert.get("resourceType") or "").strip().lower()
        resource_label = "Component" if resource_type == "component" else (resource_type.capitalize() or "Resource")
        add(resource_label, alert.get("resourceName"))
        add("Cluster", _alert_cluster_display(alert))
        add("Namespace", alert.get("namespace"))
    elif alert_type == "log":
        add("Cluster", _alert_cluster_display(alert))
        add("Namespace", alert.get("namespace"))
        add("Deployment", alert.get("deployment"))
        add("Pod", alert.get("pod"))
        add("Container", alert.get("container"))
        add("Matched pattern", alert.get("matchedPattern"))
        add("Detected at", _format_alert_timestamp(alert.get("detectedAt")))
    elif alert_type == "automation":
        add("Ticket", alert.get("ticketNumber"))
        add("Deployment", alert.get("resourceName"))
        add("Change", alert.get("changeSummary"))
        add("Cluster", _alert_cluster_display(alert))
        add("Namespace", alert.get("namespace"))
        add("Failed step", alert.get("failedStep"))
        add("Error", alert.get("error"))
        add("Jenkins build", alert.get("jenkinsBuildUrl"))
    else:
        add("Cluster", _alert_cluster_display(alert))
        add("Namespace", alert.get("namespace"))
        resource = " / ".join(
            part
            for part in (
                str(alert.get("resourceType") or "").strip(),
                str(alert.get("resourceName") or "").strip(),
            )
            if part
        )
        add("Resource", resource)
        add("Triggered conditions", _metric_conditions_summary(alert))

    add("Policy", alert.get("policyName"))
    add("Severity", _severity_style(alert)["label"])
    add("Status", str(alert.get("status") or "").capitalize())
    add("Fired at", _format_alert_timestamp(alert.get("firedAt")))
    add("Alert ID", alert.get("id"))
    return rows


def _log_snippet_text(alert: Dict[str, Any]) -> str:
    log_lines = alert.get("logLines") or []
    if log_lines:
        return "\n".join(str(line) for line in log_lines[:20])
    return str(alert.get("logSnippet") or "").strip()


def _build_alert_body(alert: Dict[str, Any]) -> str:
    style = _severity_style(alert)
    status = str(alert.get("status") or "").capitalize()
    header = f"KubeSight alert — {style['label']}" + (f" ({status})" if status else "")
    lines = [header, "", _alert_headline(alert)]

    description = str(alert.get("description") or "").strip()
    if description and description != _alert_headline(alert):
        lines += ["", description]

    rows = _alert_detail_rows(alert)
    if rows:
        width = max(len(label) for label, _ in rows) + 1
        lines += [""] + [f"{(label + ':').ljust(width + 1)}{value}" for label, value in rows]

    if (alert.get("alertType") or "metric") == "log":
        snippet = _log_snippet_text(alert)
        if snippet:
            lines += ["", "Log snippet:", "", snippet]

    lines += ["", "Sent by KubeSight alert routing."]
    return "\n".join(lines)


def _build_alert_html(alert: Dict[str, Any]) -> str:
    from html import escape

    style = _severity_style(alert)
    status = str(alert.get("status") or "").capitalize()
    headline = _alert_headline(alert)
    description = str(alert.get("description") or "").strip()

    rows_html = "".join(
        '<tr>'
        f'<td style="padding:5px 16px 5px 0;color:#6b7280;font-size:13px;white-space:nowrap;vertical-align:top;">{escape(label)}</td>'
        f'<td style="padding:5px 0;color:#111827;font-size:13px;">{escape(value)}</td>'
        "</tr>"
        for label, value in _alert_detail_rows(alert)
    )

    description_html = ""
    if description and description != headline:
        description_html = (
            f'<p style="margin:0 0 16px;color:#374151;font-size:14px;line-height:1.5;">{escape(description)}</p>'
        )

    snippet_html = ""
    if (alert.get("alertType") or "metric") == "log":
        snippet = _log_snippet_text(alert)
        if snippet:
            snippet_html = (
                '<pre style="margin:16px 0 0;padding:12px;background:#111827;color:#e5e7eb;'
                'border-radius:6px;font-size:12px;line-height:1.5;overflow-x:auto;white-space:pre-wrap;">'
                f"{escape(snippet)}</pre>"
            )

    return f"""\
<div style="margin:0;padding:24px;background:#f3f4f6;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
    <div style="padding:14px 24px;background:{style['bg']};border-bottom:1px solid {style['border']};">
      <span style="display:inline-block;padding:2px 10px;border-radius:999px;background:{style['color']};color:#ffffff;font-size:11px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;">{escape(style['label'])}</span>
      <span style="margin-left:8px;color:{style['color']};font-size:12px;font-weight:600;">{escape(status)}</span>
    </div>
    <div style="padding:20px 24px;">
      <h2 style="margin:0 0 12px;color:#111827;font-size:17px;line-height:1.4;">{escape(headline)}</h2>
      {description_html}
      <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        {rows_html}
      </table>
      {snippet_html}
    </div>
    <div style="padding:12px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#9ca3af;font-size:12px;">
      Sent by KubeSight alert routing.
    </div>
  </div>
</div>
"""


def send_email(to_address: str, subject: str, body: str, *, html_body: Optional[str] = None) -> None:
    """Send an email using the configured SMTP settings.

    Shares the same DB/env SMTP configuration as alert routing so any feature
    can reuse the management mail relay without re-implementing transport. When
    ``html_body`` is supplied it is attached as an HTML alternative (the plain
    ``body`` remains the fallback for text-only clients).
    """
    if not to_address or "@" not in to_address:
        raise EmailDeliveryError("Recipient email address is not configured.")

    if not smtp_is_configured():
        raise EmailDeliveryError(
            "SMTP is not configured. Configure SMTP in Settings → Alert Routing or set SMTP_HOST and SMTP_FROM."
        )

    settings = _smtp_settings()
    message = EmailMessage()
    message["From"] = settings["from_addr"]
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        if settings["use_ssl"]:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30) as client:
                if settings["user"]:
                    client.login(settings["user"], settings["password"])
                client.send_message(message)
            return

        with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as client:
            client.ehlo()
            if settings["use_tls"]:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if settings["user"]:
                client.login(settings["user"], settings["password"])
            client.send_message(message)
    except OSError as exc:
        raise EmailDeliveryError(f"Could not reach SMTP server: {exc}") from exc
    except smtplib.SMTPException as exc:
        raise EmailDeliveryError(f"SMTP send failed: {exc}") from exc


def send_temporary_password_email(
    to_address: str,
    *,
    username: str,
    full_name: str,
    temporary_password: str,
    expires_hours: int = 24,
    deliver: Optional[Callable[..., object]] = None,
) -> None:
    """Email a newly created user their one-time temporary password.

    ``deliver`` exists for symmetry, but **do not queue this one**: the body
    contains the plaintext temporary password, and a queued job stores its
    payload in the database until a worker drains it. Sending it inline keeps
    the secret in memory for the length of one SMTP handshake instead of at
    rest in a table. Durability is not worth that trade for a credential the
    user can have reissued.
    """
    greeting = full_name.strip() or username
    subject = "Your KubeSight account — temporary password"
    body = (
        f"Hello {greeting},\n\n"
        "An administrator has created a KubeSight account for you.\n\n"
        f"Username: {username}\n"
        f"Temporary password: {temporary_password}\n\n"
        f"This temporary password expires in {expires_hours} hours and can be used only once.\n\n"
        "On first sign-in you will be asked to:\n"
        "  1. Set a new permanent password.\n"
        "  2. Set up multi-factor authentication (MFA) with an authenticator app.\n\n"
        "After that, every sign-in will require your password and a 6-digit MFA code.\n\n"
        "If you were not expecting this email, contact your administrator.\n\n"
        "— KubeSight"
    )
    html_body = f"""\
<div style="font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937;line-height:1.6">
  <h2 style="margin:0 0 12px">Welcome to KubeSight</h2>
  <p>Hello {greeting},</p>
  <p>An administrator has created a KubeSight account for you.</p>
  <table style="border-collapse:collapse;margin:16px 0">
    <tr><td style="padding:4px 12px 4px 0;color:#6b7280">Username</td>
        <td style="padding:4px 0;font-weight:600">{username}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#6b7280">Temporary password</td>
        <td style="padding:4px 0"><code style="background:#f3f4f6;padding:4px 8px;border-radius:6px;font-size:15px">{temporary_password}</code></td></tr>
  </table>
  <p style="color:#b91c1c">This temporary password expires in {expires_hours} hours and can be used only once.</p>
  <p>On first sign-in you will be asked to set a new permanent password and configure
     multi-factor authentication (MFA). After that, every sign-in requires your
     password and a 6-digit MFA code.</p>
  <p style="color:#6b7280;font-size:13px">If you were not expecting this email, contact your administrator.</p>
  <p style="color:#6b7280;font-size:13px">— KubeSight</p>
</div>"""
    (deliver or send_email)(to_address, subject, body, html_body=html_body)


def send_login_notification_email(
    to_address: str,
    *,
    username: str,
    full_name: str,
    login_time: str,
    ip_address: str,
    user_agent: str = "",
    deliver: Optional[Callable[..., object]] = None,
) -> None:
    """Email a security notice after a successful sign-in.

    ``deliver`` swaps the transport without moving any rendering: callers that
    want the message queued rather than sent pass
    ``notification_jobs.enqueue_email``. Rendering stays in one place either way.
    """
    greeting = full_name.strip() or username
    subject = "KubeSight sign-in notification"
    ua_line = f"Browser / device: {user_agent}\n" if user_agent else ""
    body = (
        f"Hello {greeting},\n\n"
        "Your KubeSight account was just signed in.\n\n"
        f"Time: {login_time}\n"
        f"IP address: {ip_address or 'unknown'}\n"
        f"{ua_line}"
        "\nIf this was you, no action is needed. "
        "If this was not you, contact your administrator immediately.\n\n"
        "— KubeSight"
    )
    ua_html = (
        f'<tr><td style="padding:4px 12px 4px 0;color:#6b7280">Browser / device</td>'
        f'<td style="padding:4px 0">{user_agent}</td></tr>'
        if user_agent
        else ""
    )
    html_body = f"""\
<div style="font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937;line-height:1.6">
  <h2 style="margin:0 0 12px">New sign-in to your KubeSight account</h2>
  <p>Hello {greeting},</p>
  <p>Your KubeSight account was just signed in.</p>
  <table style="border-collapse:collapse;margin:16px 0">
    <tr><td style="padding:4px 12px 4px 0;color:#6b7280">Time</td>
        <td style="padding:4px 0;font-weight:600">{login_time}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#6b7280">IP address</td>
        <td style="padding:4px 0">{ip_address or 'unknown'}</td></tr>
    {ua_html}
  </table>
  <p>If this was you, no action is needed.</p>
  <p style="color:#b91c1c;font-weight:600">If this was not you, contact your administrator immediately.</p>
  <p style="color:#6b7280;font-size:13px">— KubeSight</p>
</div>"""
    (deliver or send_email)(to_address, subject, body, html_body=html_body)


def send_security_event_email(
    to_address: str,
    *,
    username: str,
    full_name: str,
    subject: str,
    headline: str,
    lines: list,
    ip_address: str = "",
    show_contact_admin: bool = True,
    deliver: Optional[Callable[..., object]] = None,
) -> None:
    """Send a generic account-security notification (locks, resets, changes).

    ``deliver`` swaps the transport; see send_login_notification_email.
    """
    greeting = full_name.strip() or username
    body_lines = [f"Hello {greeting},", "", headline, ""]
    body_lines.extend(lines)
    if ip_address:
        body_lines.append(f"IP address: {ip_address}")
    if show_contact_admin:
        body_lines.extend(["", "If this was not you, contact your administrator immediately."])
    body_lines.extend(["", "— KubeSight"])
    body = "\n".join(body_lines)

    detail_html = "".join(f"<li>{line}</li>" for line in lines if line)
    ip_html = f"<li>IP address: {ip_address}</li>" if ip_address else ""
    contact_html = (
        '<p style="color:#b91c1c;font-weight:600">If this was not you, contact your '
        "administrator immediately.</p>"
        if show_contact_admin
        else ""
    )
    html_body = f"""\
<div style="font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1f2937;line-height:1.6">
  <h2 style="margin:0 0 12px">{headline}</h2>
  <p>Hello {greeting},</p>
  <ul style="margin:12px 0;padding-left:20px">{detail_html}{ip_html}</ul>
  {contact_html}
  <p style="color:#6b7280;font-size:13px">— KubeSight</p>
</div>"""
    (deliver or send_email)(to_address, subject, body, html_body=html_body)


def send_alert_email(to_address: str, alert: Dict[str, Any], *, test: bool = False) -> None:
    if not to_address or "@" not in to_address:
        raise EmailDeliveryError("Recipient email address is not configured.")

    if not smtp_is_configured():
        raise EmailDeliveryError(
            "SMTP is not configured. Configure SMTP in Settings → Alert Routing or set SMTP_HOST and SMTP_FROM."
        )

    settings = _smtp_settings()
    message = EmailMessage()
    message["From"] = settings["from_addr"]
    message["To"] = to_address
    message["Subject"] = "KubeSight test alert" if test else _build_alert_subject(alert)
    message.set_content(
        "This is a test notification from KubeSight. Alert email delivery is working."
        if test
        else _build_alert_body(alert)
    )
    if not test:
        try:
            message.add_alternative(_build_alert_html(alert), subtype="html")
        except Exception:
            # The plain-text part is always present; never fail delivery over HTML rendering.
            pass

    try:
        if settings["use_ssl"]:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30) as client:
                if settings["user"]:
                    client.login(settings["user"], settings["password"])
                client.send_message(message)
            return

        with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as client:
            client.ehlo()
            if settings["use_tls"]:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if settings["user"]:
                client.login(settings["user"], settings["password"])
            client.send_message(message)
    except OSError as exc:
        raise EmailDeliveryError(f"Could not reach SMTP server: {exc}") from exc
    except smtplib.SMTPException as exc:
        raise EmailDeliveryError(f"SMTP send failed: {exc}") from exc
