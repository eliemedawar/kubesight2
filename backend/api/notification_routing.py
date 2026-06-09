from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

DEFAULT_ALERT_ROUTING: Dict[str, Dict[str, Any]] = {
    "email": {"enabled": False, "address": ""},
    "slack": {"enabled": False, "webhookUrl": ""},
    "webhook": {"enabled": False, "url": ""},
}


def default_alert_routing() -> Dict[str, Dict[str, Any]]:
    return deepcopy(DEFAULT_ALERT_ROUTING)


def normalize_alert_routing(raw: Any) -> Dict[str, Dict[str, Any]]:
    routing = default_alert_routing()
    if not isinstance(raw, dict):
        return routing

    for channel, defaults in DEFAULT_ALERT_ROUTING.items():
        incoming = raw.get(channel)
        if not isinstance(incoming, dict):
            continue
        routing[channel]["enabled"] = bool(incoming.get("enabled", False))
        for field in defaults:
            if field == "enabled":
                continue
            if field in incoming:
                routing[channel][field] = str(incoming.get(field) or "").strip()
    return routing


def validate_alert_routing(routing: Dict[str, Dict[str, Any]]) -> List[str]:
    errors: List[str] = []

    email = routing.get("email", {})
    if email.get("enabled"):
        address = str(email.get("address", "")).strip()
        if "@" not in address or "." not in address.split("@")[-1]:
            errors.append("A valid email address is required when email delivery is enabled.")

    slack = routing.get("slack", {})
    if slack.get("enabled"):
        webhook = str(slack.get("webhookUrl", "")).strip()
        if not webhook.startswith("https://hooks.slack.com/"):
            errors.append("Slack requires an incoming webhook URL (https://hooks.slack.com/...).")

    webhook = routing.get("webhook", {})
    if webhook.get("enabled"):
        url = str(webhook.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("Webhook requires a valid HTTP or HTTPS endpoint URL.")

    return errors


def merge_notifications(existing: Any, incoming: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    base = existing if isinstance(existing, dict) else {}
    merged = {
        "alerts": bool(base.get("alerts", True)),
        "upgrades": bool(base.get("upgrades", True)),
        "routing": normalize_alert_routing(base.get("routing")),
    }

    if "alerts" in incoming:
        merged["alerts"] = bool(incoming["alerts"])
    if "upgrades" in incoming:
        merged["upgrades"] = bool(incoming["upgrades"])
    if "routing" in incoming:
        merged["routing"] = normalize_alert_routing(incoming["routing"])
        # Legacy per-channel routing must not disable policy-based Alert Routing receivers.
        if any(cfg.get("enabled") for cfg in merged["routing"].values()):
            merged["alerts"] = True

    errors = []
    if "routing" in incoming:
        errors = validate_alert_routing(merged["routing"])

    return merged, errors


def serialize_notifications(notifications: Any) -> Dict[str, Any]:
    base = notifications if isinstance(notifications, dict) else {}
    return {
        "alerts": bool(base.get("alerts", True)),
        "upgrades": bool(base.get("upgrades", True)),
        "routing": normalize_alert_routing(base.get("routing")),
    }
