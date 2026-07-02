from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict, Optional

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


def _build_alert_subject(alert: Dict[str, Any]) -> str:
    if alert.get("alertType") == "log":
        severity = str(alert.get("severity", "alert")).upper()
        return f"[KubeSight][{severity}] Error detected in logs"
    severity = str(alert.get("severity", "alert")).upper()
    title = alert.get("title") or "KubeSight alert"
    return f"[KubeSight][{severity}] {title}"


def _build_log_alert_body(alert: Dict[str, Any]) -> str:
    lines = [
        f"Cluster: {alert.get('clusterId', '-')}",
        f"Namespace: {alert.get('namespace', '-')}",
    ]
    if alert.get("deployment"):
        lines.append(f"Deployment: {alert.get('deployment')}")
    lines.extend(
        [
            f"Pod: {alert.get('pod', '-')}",
            "",
            f"Matched Pattern:",
            str(alert.get("matchedPattern") or "-"),
            "",
            f"Detected At:",
            str(alert.get("detectedAt") or alert.get("firedAt") or "-"),
            "",
            "Log Snippet:",
            "",
            str(alert.get("logSnippet") or "-"),
        ]
    )
    return "\n".join(lines)


def _build_alert_body(alert: Dict[str, Any]) -> str:
    if alert.get("alertType") == "log":
        return _build_log_alert_body(alert)
    lines = [
        "KubeSight alert notification",
        "",
        f"Title: {alert.get('title', '-')}",
        f"Severity: {alert.get('severity', '-')}",
        f"Status: {alert.get('status', '-')}",
        f"Cluster: {alert.get('clusterId', '-')}",
        f"Namespace: {alert.get('namespace', '-')}",
        f"Resource type: {alert.get('resourceType', '-')}",
        f"Description: {alert.get('description', '-')}",
        f"Fired at: {alert.get('firedAt', '-')}",
        f"Alert ID: {alert.get('id', '-')}",
        "",
        "This message was sent by KubeSight alert routing.",
    ]
    return "\n".join(lines)


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
) -> None:
    """Email a newly created user their one-time temporary password."""
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
    send_email(to_address, subject, body, html_body=html_body)


def send_login_notification_email(
    to_address: str,
    *,
    username: str,
    full_name: str,
    login_time: str,
    ip_address: str,
    user_agent: str = "",
) -> None:
    """Email a security notice after a successful sign-in."""
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
    send_email(to_address, subject, body, html_body=html_body)


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
) -> None:
    """Send a generic account-security notification (locks, resets, changes)."""
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
    send_email(to_address, subject, body, html_body=html_body)


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
