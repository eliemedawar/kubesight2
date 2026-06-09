from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Dict


class EmailDeliveryError(RuntimeError):
    pass


def smtp_is_configured() -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    from_addr = os.getenv("SMTP_FROM", "").strip()
    return bool(host and from_addr)


def _smtp_settings() -> Dict[str, Any]:
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from_addr": os.getenv("SMTP_FROM", "").strip(),
        "use_tls": os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"},
        "use_ssl": os.getenv("SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "on"},
    }


def _build_alert_subject(alert: Dict[str, Any]) -> str:
    severity = str(alert.get("severity", "alert")).upper()
    title = alert.get("title") or "KubeSight alert"
    return f"[KubeSight][{severity}] {title}"


def _build_alert_body(alert: Dict[str, Any]) -> str:
    lines = [
        "KubeSight alert notification",
        "",
        f"Title: {alert.get('title', '-')}",
        f"Severity: {alert.get('severity', '-')}",
        f"Status: {alert.get('status', '-')}",
        f"Cluster: {alert.get('clusterId', '-')}",
        f"Description: {alert.get('description', '-')}",
        f"Fired at: {alert.get('firedAt', '-')}",
        f"Alert ID: {alert.get('id', '-')}",
        "",
        "This message was sent because email routing is enabled in KubeSight settings.",
    ]
    return "\n".join(lines)


def send_alert_email(to_address: str, alert: Dict[str, Any], *, test: bool = False) -> None:
    if not to_address or "@" not in to_address:
        raise EmailDeliveryError("Recipient email address is not configured.")

    if not smtp_is_configured():
        raise EmailDeliveryError(
            "SMTP is not configured. Set SMTP_HOST and SMTP_FROM in the backend environment."
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
