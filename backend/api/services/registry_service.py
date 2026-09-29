"""Manage linked image registries and check image availability before deploy.

CRUD + serialization for :class:`RegistryConnection`, plus the deploy-time glue:
match a container image reference to a configured registry and ask that registry
(over the Docker V2 API in :mod:`registry_client`) whether the image exists.

The public entry points used by the deploy flow are :func:`check_image` (one
image) and :func:`check_images` (a batch, returning ✅/⚠️/❌ checks and whether
the deploy should be blocked).

Clusters can be linked to registries (:class:`RegistryClusterLink`). When the
deploy's cluster has links, an image is looked up by repository + tag in each of
that cluster's registries and must be confirmed by at least one of them — a
missing image OR an unreachable registry blocks when enforcement is ``block``.
A cluster with no links keeps the host-matching check across all registries.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..db import db
from ..models import RegistryClusterLink, RegistryConnection
from ..secret_encryption import decrypt_secret, encrypt_secret
from . import registry_client
from .registry_client import FOUND, NOT_FOUND, UNREACHABLE

VALID_ENFORCEMENT = {"off", "warn", "block"}
VALID_AUTH_MODES = {"none", "basic"}
VALID_TYPES = {"nexus", "generic"}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _normalize_hosts(value: Any) -> List[str]:
    """Parse a list or comma/space/newline-separated string into unique hosts."""
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = re.split(r"[\s,;]+", str(value or ""))
    out: List[str] = []
    seen: set = set()
    for part in parts:
        host = part.strip().lower().rstrip("/")
        # Tolerate a pasted scheme (https://host) or trailing path.
        if "://" in host:
            host = host.split("://", 1)[1]
        host = host.split("/", 1)[0]
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


def _connection_hosts(row: RegistryConnection) -> List[str]:
    """All image-reference hosts this connection matches: base URL host + aliases."""
    hosts = _normalize_hosts(row.image_hosts)
    base_host = registry_client.registry_host_of(row.base_url).lower()
    if base_host and base_host not in hosts:
        hosts.append(base_host)
    return hosts


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
        "imageHosts": _normalize_hosts(row.image_hosts),
        "matchHosts": _connection_hosts(row),
        "username": row.username or "",
        "passwordConfigured": bool(row.password_encrypted),
        "verifyTls": bool(row.verify_tls),
        "caCertConfigured": bool(row.ca_cert),
        "enforcement": row.enforcement or "block",
        "enabled": bool(row.enabled),
        "clusterIds": linked_cluster_ids(row.id),
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

    if "imageHosts" in payload:
        hosts = _normalize_hosts(payload.get("imageHosts"))
        row.image_hosts = ",".join(hosts) or None


def create_connection(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = RegistryConnection()
    _apply_payload(row, payload)
    db.session.add(row)
    db.session.flush()
    if "clusterIds" in payload:
        _set_registry_clusters(row.id, payload.get("clusterIds"))
    db.session.commit()
    return serialize(row)


def update_connection(connection_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_connection(connection_id)
    _apply_payload(row, payload)
    db.session.add(row)
    if "clusterIds" in payload:
        _set_registry_clusters(row.id, payload.get("clusterIds"))
    db.session.commit()
    return serialize(row)


def delete_connection(connection_id: int) -> None:
    row = get_connection(connection_id)
    RegistryClusterLink.query.filter_by(registry_id=row.id).delete()
    db.session.delete(row)
    db.session.commit()


# ---------------------------------------------------------------------------
# Cluster <-> registry links
# ---------------------------------------------------------------------------

def _normalize_cluster_ids(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        value = re.split(r"[\s,;]+", str(value or ""))
    out: List[str] = []
    for item in value:
        cid = str(item or "").strip()
        if cid and cid not in out:
            out.append(cid)
    return out


def linked_cluster_ids(registry_id: int) -> List[str]:
    rows = (
        RegistryClusterLink.query.filter_by(registry_id=registry_id)
        .order_by(RegistryClusterLink.cluster_id.asc())
        .all()
    )
    return [row.cluster_id for row in rows]


def cluster_registry_ids(cluster_id: Optional[str]) -> List[int]:
    """Ids of every registry linked to ``cluster_id`` (enabled or not)."""
    cid = str(cluster_id or "").strip()
    if not cid:
        return []
    rows = (
        RegistryClusterLink.query.filter_by(cluster_id=cid)
        .order_by(RegistryClusterLink.registry_id.asc())
        .all()
    )
    return [row.registry_id for row in rows]


def _set_registry_clusters(registry_id: int, cluster_ids: Any) -> None:
    """Replace the clusters linked to one registry (does not commit)."""
    wanted = _normalize_cluster_ids(cluster_ids)
    RegistryClusterLink.query.filter_by(registry_id=registry_id).delete()
    for cid in wanted:
        db.session.add(RegistryClusterLink(registry_id=registry_id, cluster_id=cid))


def set_cluster_registries(cluster_id: str, registry_ids: Any) -> List[int]:
    """Replace the registries linked to one cluster. Raises ValueError/LookupError."""
    cid = str(cluster_id or "").strip()
    if not cid:
        raise ValueError("A cluster id is required.")
    if not isinstance(registry_ids, (list, tuple)):
        raise ValueError("registryIds must be a list.")
    wanted: List[int] = []
    for item in registry_ids:
        try:
            rid = int(item)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid registry id: {item!r}") from None
        if rid not in wanted:
            wanted.append(rid)
    if wanted:
        known = {
            row.id
            for row in RegistryConnection.query.filter(RegistryConnection.id.in_(wanted)).all()
        }
        missing = [rid for rid in wanted if rid not in known]
        if missing:
            raise LookupError(f"Registry not found: {missing[0]}")
    RegistryClusterLink.query.filter_by(cluster_id=cid).delete()
    for rid in wanted:
        db.session.add(RegistryClusterLink(registry_id=rid, cluster_id=cid))
    db.session.commit()
    return cluster_registry_ids(cid)


def cluster_links_summary() -> Dict[str, List[int]]:
    """``{cluster_id: [registry ids]}`` for every cluster that has links."""
    out: Dict[str, List[int]] = {}
    for row in RegistryClusterLink.query.order_by(RegistryClusterLink.registry_id.asc()).all():
        out.setdefault(row.cluster_id, []).append(row.registry_id)
    return out


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
    """Ping the registry's base ``/v2/`` endpoint to confirm reachability + creds."""
    row = get_connection(connection_id)
    status, message = registry_client.ping(
        row.base_url,
        username=row.username,
        password=decrypt_secret(row.password_encrypted or ""),
        verify_tls=bool(row.verify_tls),
        ca_cert=row.ca_cert,
    )
    ok = status == FOUND
    result_status = "ok" if ok else "error"
    result_message = message
    _record_test(row, result_status, result_message)
    return {"status": result_status, "message": result_message, **serialize(row)}


def _enabled_connections() -> List[RegistryConnection]:
    return (
        RegistryConnection.query.filter(RegistryConnection.enabled.is_(True))
        .order_by(RegistryConnection.id.asc())
        .all()
    )


def match_connection(
    registry_host: str, preferred_id: Optional[int] = None
) -> Optional[RegistryConnection]:
    """The enabled connection matching ``registry_host`` (base URL host or alias).

    Several connections can claim the same image host (e.g. one DNS name fronting
    two registry instances); by default the first-created enabled match wins.
    ``preferred_id`` breaks that tie: when that connection is enabled AND matches
    the host, it is chosen instead. A preferred connection that is disabled,
    deleted, or doesn't own the host falls back to the default scan.
    """
    host = (registry_host or "").strip().lower()
    if not host:
        return None
    rows = _enabled_connections()
    if preferred_id:
        for row in rows:
            if row.id == int(preferred_id) and host in _connection_hosts(row):
                return row
    for row in rows:
        if host in _connection_hosts(row):
            return row
    return None


def allowed_registry_hosts() -> List[str]:
    """Every image host of all enabled connections — feeds the deploy allow-list."""
    hosts: set = set()
    for row in _enabled_connections():
        hosts.update(_connection_hosts(row))
    return sorted(h for h in hosts if h)


def _manifest_status(conn: RegistryConnection, repository: str, reference: str) -> Tuple[str, str]:
    return registry_client.check_manifest(
        conn.base_url,
        repository,
        reference,
        username=conn.username,
        password=decrypt_secret(conn.password_encrypted or ""),
        verify_tls=bool(conn.verify_tls),
        ca_cert=conn.ca_cert,
    )


def _check_in_cluster_registries(
    image: str, parsed: registry_client.ParsedImage, registry_ids: List[int]
) -> Dict[str, Any]:
    """Look ``image`` up (repository + tag) in each registry linked to the cluster.

    Found in any one → ``found``. Otherwise the image is ``not_found`` when every
    registry answered, or ``unreachable`` when at least one couldn't be asked —
    and both block when any checked registry enforces ``block``: the cluster's
    registries could not confirm the image, so it must not be deployed.
    """
    rows = (
        RegistryConnection.query.filter(RegistryConnection.id.in_(registry_ids))
        .order_by(RegistryConnection.id.asc())
        .all()
    )
    active = [r for r in rows if r.enabled and (r.enforcement or "block") != "off"]
    if not active:
        return {
            "image": image,
            "status": "no_connection",
            "message": "None of this cluster's registries is enabled for image checks; skipping.",
            "registry": "",
            "enforcement": "off",
            "blocking": False,
            "clusterScoped": True,
            "registries": [],
        }

    attempts: List[Dict[str, Any]] = []
    for conn in active:
        status, message = _manifest_status(conn, parsed.repository, parsed.reference)
        attempt = {
            "id": conn.id,
            "name": conn.name or "",
            "registry": registry_client.registry_host_of(conn.base_url),
            "status": status,
            "message": message,
        }
        attempts.append(attempt)
        if status == FOUND:
            return {
                "image": image,
                "status": FOUND,
                "message": (
                    f"{parsed.repository}:{parsed.reference} exists in "
                    f"{conn.name or attempt['registry']}."
                ),
                "registry": attempt["registry"],
                "enforcement": conn.enforcement or "block",
                "blocking": False,
                "clusterScoped": True,
                "registries": attempts,
            }

    enforcement = "block" if any((r.enforcement or "block") == "block" for r in active) else "warn"
    names = ", ".join(a["name"] or a["registry"] for a in attempts)
    unreachable = [a for a in attempts if a["status"] != NOT_FOUND]
    if unreachable:
        status = UNREACHABLE
        message = (
            f"{parsed.repository}:{parsed.reference} could not be confirmed in this cluster's "
            f"registries ({names}) — "
            + "; ".join(f"{a['name'] or a['registry']}: {a['message']}" for a in unreachable)
        )
    else:
        status = NOT_FOUND
        message = (
            f"{parsed.repository}:{parsed.reference} was not found in any of this "
            f"cluster's registries ({names})."
        )
    return {
        "image": image,
        "status": status,
        "message": message,
        "registry": ", ".join(a["registry"] for a in attempts),
        "enforcement": enforcement,
        "blocking": enforcement == "block",
        "clusterScoped": True,
        "registries": attempts,
    }


def check_image(
    image: str,
    preferred_connection_id: Optional[int] = None,
    cluster_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Check one image reference against the registries that should hold it.

    Returns ``{image, status, message, registry, enforcement, blocking}`` where
    status is ``found | not_found | unreachable | no_connection`` and
    ``blocking`` says whether this result stops a deploy.

    With a ``cluster_id`` that has linked registries, the image is looked up in
    those registries (see :func:`_check_in_cluster_registries`). Otherwise it is
    matched by host to one registry; ``no_connection`` means no linked registry
    owns that image's host — the check simply doesn't apply.
    ``preferred_connection_id`` tie-breaks between connections claiming the same
    host (see :func:`match_connection`).
    """
    parsed = registry_client.parse_image_reference(image)
    if parsed is None:
        return {"image": image, "status": "no_connection", "message": "No image specified.",
                "registry": "", "enforcement": "off", "blocking": False}

    registry_ids = cluster_registry_ids(cluster_id) if cluster_id else []
    if registry_ids:
        return _check_in_cluster_registries(image, parsed, registry_ids)

    conn = (
        match_connection(parsed.registry, preferred_id=preferred_connection_id)
        if parsed.has_registry
        else None
    )
    if conn is None:
        return {
            "image": image,
            "status": "no_connection",
            "message": "No linked registry matches this image; skipping the availability check.",
            "registry": parsed.registry,
            "enforcement": "off",
            "blocking": False,
        }

    status, message = _manifest_status(conn, parsed.repository, parsed.reference)
    enforcement = conn.enforcement or "block"
    return {
        "image": image,
        "status": status,
        "message": message,
        "registry": registry_client.registry_host_of(conn.base_url),
        "enforcement": enforcement,
        # Host-matched mode: only a definite "missing" blocks; an unreachable
        # registry leaves the decision to Kubernetes.
        "blocking": status == NOT_FOUND and enforcement == "block",
    }


def check_images(
    images: Iterable[str], cluster_id: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], bool]:
    """Check a batch of images. Returns (checks, blocking).

    ``blocking`` is True when any single check blocks (see :func:`check_image`).
    Duplicate image references are checked once.
    """
    checks: List[Dict[str, Any]] = []
    blocking = False
    seen: set = set()
    for image in images:
        key = str(image or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result = check_image(key, cluster_id=cluster_id)
        checks.append(result)
        if result.get("blocking"):
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
