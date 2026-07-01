"""Manage linked image registries and check image availability before deploy.

CRUD + serialization for :class:`RegistryConnection`, plus the deploy-time glue:
match a container image reference to a configured registry and ask that registry
(over the Docker V2 API in :mod:`registry_client`) whether the image exists.

The public entry points used by the deploy flow are :func:`check_image` (one
image) and :func:`check_images` (a batch, returning ✅/⚠️/❌ checks and whether
the deploy should be blocked).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..db import db
from ..models import RegistryConnection
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import registry_client
from .registry_client import FOUND, NOT_FOUND, UNREACHABLE

VALID_ENFORCEMENT = {"off", "warn", "block"}
VALID_AUTH_MODES = {"none", "basic"}
VALID_TYPES = {"nexus", "generic"}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# CRUD + serialization
# ---------------------------------------------------------------------------

def serialize(row: RegistryConnection) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name or "",
        "registryType": row.registry_type or "nexus",
        "baseUrl": row.base_url or "",
        "authMode": row.auth_mode or "basic",
        "username": row.username or "",
        "passwordConfigured": bool(row.password_encrypted),
        "verifyTls": bool(row.verify_tls),
        "caCertConfigured": bool(row.ca_cert),
        "enforcement": row.enforcement or "block",
        "enabled": bool(row.enabled),
        "host": registry_client.registry_host_of(row.base_url),
        "lastTestAt": _iso(row.last_test_at),
        "lastTestStatus": row.last_test_status,
        "lastTestMessage": row.last_test_message,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_connections() -> List[Dict[str, Any]]:
    rows = RegistryConnection.query.order_by(RegistryConnection.id.asc()).all()
    return [serialize(row) for row in rows]


def get_connection(connection_id: int) -> RegistryConnection:
    row = RegistryConnection.query.get(int(connection_id))
    if not row:
        raise LookupError("Registry connection not found.")
    return row


def _apply_payload(row: RegistryConnection, payload: Dict[str, Any]) -> None:
    """Validate + copy a payload onto ``row`` (does not commit). Raises ValueError."""
    errors: List[str] = []

    name = str(payload.get("name", row.name or "")).strip()
    if not name:
        errors.append("A name is required.")

    base_url = str(payload.get("baseUrl", row.base_url or "")).strip()
    if not base_url:
        errors.append("A registry URL is required.")

    registry_type = str(payload.get("registryType", row.registry_type or "nexus")).strip().lower()
    if registry_type not in VALID_TYPES:
        errors.append("Registry type must be 'nexus' or 'generic'.")

    auth_mode = str(payload.get("authMode", row.auth_mode or "basic")).strip().lower()
    if auth_mode not in VALID_AUTH_MODES:
        errors.append("Auth mode must be 'none' or 'basic'.")

    enforcement = str(payload.get("enforcement", row.enforcement or "block")).strip().lower()
    if enforcement not in VALID_ENFORCEMENT:
        errors.append("Enforcement must be 'off', 'warn', or 'block'.")

    username = str(payload.get("username", row.username or "")).strip()
    if auth_mode == "basic" and not username and not row.username:
        errors.append("Basic auth requires a username.")

    if errors:
        raise ValueError(" ".join(errors))

    row.name = name
    row.base_url = base_url
    row.registry_type = registry_type
    row.auth_mode = auth_mode
    row.enforcement = enforcement
    row.username = username
    row.verify_tls = bool(payload.get("verifyTls", row.verify_tls))
    if payload.get("enabled") is not None:
        row.enabled = bool(payload.get("enabled"))

    password = payload.get("password")
    if password is not None and str(password).strip():
        row.password_encrypted = encrypt_secret(str(password).strip())
    if payload.get("clearPassword"):
        row.password_encrypted = None

    ca_cert = payload.get("caCert")
    if ca_cert is not None:
        row.ca_cert = str(ca_cert).strip() or None


def create_connection(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = RegistryConnection()
    _apply_payload(row, payload)
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def update_connection(connection_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_connection(connection_id)
    _apply_payload(row, payload)
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def delete_connection(connection_id: int) -> None:
    row = get_connection(connection_id)
    db.session.delete(row)
    db.session.commit()


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------

def _record_test(row: RegistryConnection, status: str, message: str) -> None:
    row.last_test_at = datetime.now(timezone.utc)
    row.last_test_status = status
    row.last_test_message = message
    db.session.add(row)
    db.session.commit()


def test_connection(connection_id: int) -> Dict[str, Any]:
    """Ping ``/v2/`` on the registry to confirm reachability + credentials."""
    row = get_connection(connection_id)
    status, message = registry_client.check_manifest(
        row.base_url,
        # A repo/tag that (almost) never exists: a 404 still proves the endpoint
        # answered and authenticated, which is all a connection test needs.
        "kubesight/_connectivity_probe",
        "does-not-exist",
        username=row.username,
        password=decrypt_secret(row.password_encrypted or ""),
        verify_tls=bool(row.verify_tls),
        ca_cert=row.ca_cert,
    )
    # FOUND/NOT_FOUND both mean the registry answered us -> connection OK.
    ok = status in (FOUND, NOT_FOUND)
    result_status = "ok" if ok else "error"
    result_message = "Connection successful." if ok else message
    _record_test(row, result_status, result_message)
    return {"status": result_status, "message": result_message, **serialize(row)}


def _enabled_connections() -> List[RegistryConnection]:
    return (
        RegistryConnection.query.filter(RegistryConnection.enabled.is_(True))
        .order_by(RegistryConnection.id.asc())
        .all()
    )


def match_connection(registry_host: str) -> Optional[RegistryConnection]:
    """The enabled connection whose host matches ``registry_host`` (or None)."""
    host = (registry_host or "").strip().lower()
    if not host:
        return None
    for row in _enabled_connections():
        if registry_client.registry_host_of(row.base_url).lower() == host:
            return row
    return None


def allowed_registry_hosts() -> List[str]:
    """Hosts of all enabled connections — feeds the deploy allow-list."""
    hosts = {registry_client.registry_host_of(r.base_url) for r in _enabled_connections()}
    return sorted(h for h in hosts if h)


def check_image(image: str) -> Dict[str, Any]:
    """Check one image reference against its matching registry.

    Returns ``{image, status, message, registry, enforcement}`` where status is
    ``found | not_found | unreachable | no_connection``. ``no_connection`` means
    no linked registry owns that image's host — the check simply doesn't apply.
    """
    parsed = registry_client.parse_image_reference(image)
    if parsed is None:
        return {"image": image, "status": "no_connection", "message": "No image specified.",
                "registry": "", "enforcement": "off"}

    conn = match_connection(parsed.registry) if parsed.has_registry else None
    if conn is None:
        return {
            "image": image,
            "status": "no_connection",
            "message": "No linked registry matches this image; skipping the availability check.",
            "registry": parsed.registry,
            "enforcement": "off",
        }

    status, message = registry_client.check_manifest(
        conn.base_url,
        parsed.repository,
        parsed.reference,
        username=conn.username,
        password=decrypt_secret(conn.password_encrypted or ""),
        verify_tls=bool(conn.verify_tls),
        ca_cert=conn.ca_cert,
    )
    return {
        "image": image,
        "status": status,
        "message": message,
        "registry": registry_client.registry_host_of(conn.base_url),
        "enforcement": conn.enforcement or "block",
    }


def check_images(images: Iterable[str]) -> Tuple[List[Dict[str, Any]], bool]:
    """Check a batch of images. Returns (checks, blocking).

    ``blocking`` is True when any image is missing from a registry whose
    enforcement is ``block``. Duplicate image references are checked once.
    """
    checks: List[Dict[str, Any]] = []
    blocking = False
    seen: set = set()
    for image in images:
        key = str(image or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result = check_image(key)
        checks.append(result)
        if result["status"] == NOT_FOUND and result.get("enforcement") == "block":
            blocking = True
    return checks, blocking


# Container image references live at these paths in a workload's pod spec.
_POD_SPEC_PARENTS = ("spec",)


def images_from_documents(documents: Iterable[Dict[str, Any]]) -> List[str]:
    """Every container image referenced across a list of parsed K8s manifests."""
    images: List[str] = []

    def _collect(pod_spec: Dict[str, Any]) -> None:
        if not isinstance(pod_spec, dict):
            return
        for field in ("initContainers", "containers", "ephemeralContainers"):
            for container in pod_spec.get(field) or []:
                if isinstance(container, dict) and container.get("image"):
                    images.append(str(container["image"]).strip())

    for doc in documents or []:
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec")
        if not isinstance(spec, dict):
            continue
        # Deployment/StatefulSet/DaemonSet/ReplicaSet/Job: spec.template.spec
        template = spec.get("template")
        if isinstance(template, dict) and isinstance(template.get("spec"), dict):
            _collect(template["spec"])
        # CronJob: spec.jobTemplate.spec.template.spec
        job_template = spec.get("jobTemplate")
        if isinstance(job_template, dict):
            job_spec = job_template.get("spec")
            if isinstance(job_spec, dict) and isinstance(job_spec.get("template"), dict):
                _collect(job_spec["template"].get("spec") or {})
        # Bare Pod: spec.containers
        if doc.get("kind") == "Pod":
            _collect(spec)

    # De-dupe while preserving order.
    out: List[str] = []
    seen: set = set()
    for image in images:
        if image and image not in seen:
            seen.add(image)
            out.append(image)
    return out
