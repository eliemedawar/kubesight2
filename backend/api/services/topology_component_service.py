"""Business logic for reusable Topology Components.

A component (e.g. "WAF", "API Gateway") is a named, reusable building block that
can be dropped into an application service's topology. Each component carries an
optional health check:

* ``http``    — outbound GET to a URL; 2xx/3xx healthy, 5xx unhealthy.
* ``tcp``     — outbound TCP connect to host:port.
* ``webhook`` — inbound heartbeat; healthy while a heartbeat arrived within the
                freshness window, otherwise unhealthy/unknown.
* ``none``    — no automated check (status stays "unknown").
"""

from __future__ import annotations

import secrets
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import TopologyComponent

CHECK_TYPES = ("none", "http", "tcp", "webhook")
STATUSES = ("healthy", "degraded", "unhealthy", "unknown")
_HTTP_TIMEOUT_SECONDS = 5
_TCP_TIMEOUT_SECONDS = 5
_DEFAULT_HEARTBEAT_INTERVAL = 300


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _to_dict(c: TopologyComponent, *, include_webhook: bool = False) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": c.id,
        "name": c.name,
        "category": c.category,
        "description": c.description,
        "checkType": c.check_type,
        "healthCheckUrl": c.health_check_url,
        "tcpHost": c.tcp_host,
        "tcpPort": c.tcp_port,
        "heartbeatIntervalSeconds": c.heartbeat_interval_seconds,
        "lastHeartbeatAt": c.last_heartbeat_at.isoformat() if c.last_heartbeat_at else None,
        "lastStatus": c.last_status or "unknown",
        "lastMessage": c.last_message,
        "lastCheckedAt": c.last_checked_at.isoformat() if c.last_checked_at else None,
        "createdAt": c.created_at.isoformat() if c.created_at else None,
        "updatedAt": c.updated_at.isoformat() if c.updated_at else None,
    }
    # The webhook token is a secret used to post heartbeats; only return it to
    # users who can manage the component (create/update), never in bulk lists.
    if include_webhook and c.check_type == "webhook":
        data["webhookToken"] = c.webhook_token
    return data


def component_summary(c: TopologyComponent) -> Dict[str, Any]:
    """Compact form used when embedding a component on a topology node."""
    return {
        "id": c.id,
        "name": c.name,
        "category": c.category,
        "description": c.description,
        "checkType": c.check_type,
        "lastStatus": c.last_status or "unknown",
        "lastCheckedAt": c.last_checked_at.isoformat() if c.last_checked_at else None,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate(payload: Dict[str, Any], *, component_id: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Component name is required."
    if len(name) > 120:
        return None, "Component name must be 120 characters or less."
    existing = TopologyComponent.query.filter(db.func.lower(TopologyComponent.name) == name.lower()).first()
    if existing and existing.id != component_id:
        return None, f"A component named '{name}' already exists."

    check_type = (payload.get("checkType") or "none").strip().lower()
    if check_type not in CHECK_TYPES:
        return None, "Check type must be one of: none, http, tcp, webhook."

    url = (payload.get("healthCheckUrl") or "").strip() or None
    tcp_host = (payload.get("tcpHost") or "").strip() or None
    tcp_port = _coerce_int(payload.get("tcpPort"))
    interval = _coerce_int(payload.get("heartbeatIntervalSeconds")) or _DEFAULT_HEARTBEAT_INTERVAL

    if check_type == "http":
        if not url:
            return None, "A health check URL is required for an HTTP check."
        if not (url.startswith("http://") or url.startswith("https://")):
            return None, "Health check URL must start with http:// or https://."
    elif check_type == "tcp":
        if not tcp_host or not tcp_port:
            return None, "A host and port are required for a TCP check."
        if tcp_port < 1 or tcp_port > 65535:
            return None, "TCP port must be between 1 and 65535."
    elif check_type == "webhook":
        if interval < 10:
            return None, "Heartbeat interval must be at least 10 seconds."

    return {
        "name": name,
        "category": (payload.get("category") or "").strip() or None,
        "description": (payload.get("description") or "").strip() or None,
        "check_type": check_type,
        "health_check_url": url if check_type == "http" else None,
        "tcp_host": tcp_host if check_type == "tcp" else None,
        "tcp_port": tcp_port if check_type == "tcp" else None,
        "heartbeat_interval_seconds": interval if check_type == "webhook" else None,
    }, None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_components() -> Dict[str, Any]:
    rows = TopologyComponent.query.order_by(TopologyComponent.name.asc()).all()
    return {"items": [_to_dict(c) for c in rows], "count": len(rows)}


def get_component(component_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    c = TopologyComponent.query.get(component_id)
    if not c:
        return None, "Component not found.", 404
    return _to_dict(c, include_webhook=True), None, 200


def create_component(payload: Dict[str, Any], actor_user_id: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    clean, error = _validate(payload)
    if error:
        return None, error, 400
    component = TopologyComponent(created_by=actor_user_id, **clean)
    if component.check_type == "webhook":
        component.webhook_token = secrets.token_urlsafe(24)
    db.session.add(component)
    db.session.commit()
    log_audit(
        "topology_component_created",
        actor_user_id=actor_user_id,
        target_type="topology_component",
        target_id=str(component.id),
        details={"name": component.name, "checkType": component.check_type},
    )
    return _to_dict(component, include_webhook=True), None, 201


def update_component(component_id: int, payload: Dict[str, Any], actor_user_id: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    component = TopologyComponent.query.get(component_id)
    if not component:
        return None, "Component not found.", 404
    clean, error = _validate(payload, component_id=component_id)
    if error:
        return None, error, 400
    for key, value in clean.items():
        setattr(component, key, value)
    # Ensure a webhook token exists when switching to webhook; clear stale state
    # otherwise so the status doesn't reflect an old check type.
    if component.check_type == "webhook":
        if not component.webhook_token:
            component.webhook_token = secrets.token_urlsafe(24)
    component.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    log_audit(
        "topology_component_updated",
        actor_user_id=actor_user_id,
        target_type="topology_component",
        target_id=str(component.id),
        details={"name": component.name, "checkType": component.check_type},
    )
    return _to_dict(component, include_webhook=True), None, 200


def delete_component(component_id: int, actor_user_id: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    component = TopologyComponent.query.get(component_id)
    if not component:
        return None, "Component not found.", 404
    # Detach the component from any topology nodes that reference it (keep the
    # node, drop the link) so deleting a component never breaks a saved topology.
    from ..models import ApplicationServiceTopologyNode

    for node in ApplicationServiceTopologyNode.query.filter_by(component_id=component_id).all():
        node.component_id = None
    name = component.name
    db.session.delete(component)
    db.session.commit()
    log_audit(
        "topology_component_deleted",
        actor_user_id=actor_user_id,
        target_type="topology_component",
        target_id=str(component_id),
        details={"name": name},
    )
    return {"id": component_id, "deleted": True}, None, 200


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def _check_http(url: str) -> Tuple[str, str]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "KubeSight-Component/1.0"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            code = getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (urllib.error.URLError, TimeoutError, Exception) as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        if "timed out" in str(reason).lower():
            return "degraded", "Health check URL timed out."
        return "unhealthy", f"Health check failed: {reason}."
    latency = int((time.monotonic() - start) * 1000)
    if 200 <= code <= 399:
        return "healthy", f"HTTP {code} in {latency}ms."
    if 500 <= code <= 599:
        return "unhealthy", f"HTTP {code}."
    return "degraded", f"HTTP {code}."


def _check_tcp(host: str, port: int) -> Tuple[str, str]:
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=_TCP_TIMEOUT_SECONDS):
            latency = int((time.monotonic() - start) * 1000)
            return "healthy", f"Connected to {host}:{port} in {latency}ms."
    except (socket.timeout, TimeoutError):
        return "unhealthy", f"Connection to {host}:{port} timed out."
    except OSError as exc:
        return "unhealthy", f"Cannot connect to {host}:{port}: {exc}."


def _check_webhook(component: TopologyComponent) -> Tuple[str, str]:
    if not component.last_heartbeat_at:
        return "unknown", "No heartbeat received yet."
    interval = component.heartbeat_interval_seconds or _DEFAULT_HEARTBEAT_INTERVAL
    last = component.last_heartbeat_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age <= interval:
        return "healthy", f"Heartbeat {int(age)}s ago."
    return "unhealthy", f"No heartbeat for {int(age)}s (limit {interval}s)."


def run_health_check(component_id: int, actor_user_id: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    component = TopologyComponent.query.get(component_id)
    if not component:
        return None, "Component not found.", 404

    if component.check_type == "http":
        status, message = _check_http(component.health_check_url)
    elif component.check_type == "tcp":
        status, message = _check_tcp(component.tcp_host, component.tcp_port)
    elif component.check_type == "webhook":
        status, message = _check_webhook(component)
    else:
        status, message = "unknown", "No health check configured."

    component.last_status = status
    component.last_message = message
    component.last_checked_at = datetime.now(timezone.utc)
    db.session.commit()
    log_audit(
        "topology_component_checked",
        actor_user_id=actor_user_id,
        target_type="topology_component",
        target_id=str(component.id),
        details={"name": component.name, "status": status},
    )
    return _to_dict(component, include_webhook=True), None, 200


def record_heartbeat(component_id: int, token: str, status: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Inbound webhook: an external monitor reports the component is alive.

    Token-gated (no session auth) so external systems can post heartbeats.
    """
    component = TopologyComponent.query.get(component_id)
    if not component or component.check_type != "webhook":
        return None, "Webhook component not found.", 404
    if not component.webhook_token or not token or not secrets.compare_digest(token, component.webhook_token):
        return None, "Invalid webhook token.", 403

    now = datetime.now(timezone.utc)
    component.last_heartbeat_at = now
    reported = (status or "healthy").strip().lower()
    component.last_status = reported if reported in STATUSES else "healthy"
    component.last_message = "Heartbeat received."
    component.last_checked_at = now
    db.session.commit()
    return {"id": component.id, "status": component.last_status, "receivedAt": now.isoformat()}, None, 200
