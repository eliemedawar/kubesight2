"""vSphere connection CRUD + cached inventory for the Cluster Builder VM picker.

Follows ``registry_service``: encrypted password at rest, ``test`` records
status/error/last-tested on the row, secrets never serialized back out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..db import db
from ..models import VSphereConnection
from ..secret_encryption import decrypt_secret, encrypt_secret
from ..ttl_cache import TTLCache
from . import vsphere_client
from .vsphere_client import VSphereConfig, VSphereError

_INVENTORY_TTL_SECONDS = 60
_INVENTORY_STALE_TTL_SECONDS = 240
_inventory_cache = TTLCache("vsphere-inventory")

# Test seam: replaces the inventory fetcher without monkeypatching urllib.
_inventory_fetcher: Optional[Callable[[VSphereConfig], List[Dict[str, Any]]]] = None


def set_inventory_fetcher(fetcher) -> None:
    global _inventory_fetcher
    _inventory_fetcher = fetcher
    _inventory_cache.invalidate()


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def serialize(row: VSphereConnection) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "baseUrl": row.base_url,
        "username": row.username,
        "passwordConfigured": bool(row.password_cipher),
        "skipTlsVerify": row.skip_tls_verify,
        "caConfigured": bool(row.ca_pem),
        "datacenterFilter": row.datacenter_filter,
        "folderFilter": row.folder_filter,
        "isActive": row.is_active,
        "lastConnectionStatus": row.last_connection_status,
        "lastConnectionError": row.last_connection_error,
        "lastTestedAt": _iso(row.last_tested_at),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def list_connections() -> List[Dict[str, Any]]:
    rows = VSphereConnection.query.order_by(VSphereConnection.name.asc()).all()
    return [serialize(row) for row in rows]


def get_connection(connection_id: int) -> VSphereConnection:
    row = db.session.get(VSphereConnection, connection_id)
    if row is None:
        raise LookupError("vSphere connection not found.")
    return row


def _apply_payload(row: VSphereConnection, payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", row.name or "")).strip()
    base_url = str(payload.get("baseUrl", row.base_url or "")).strip()
    username = str(payload.get("username", row.username or "")).strip()
    if not name:
        raise ValueError("name is required.")
    if not base_url:
        raise ValueError("baseUrl is required (vCenter URL or hostname).")
    if not username:
        raise ValueError("username is required.")
    row.name = name
    row.base_url = base_url
    row.username = username
    row.skip_tls_verify = bool(payload.get("skipTlsVerify", row.skip_tls_verify))
    if "caPem" in payload:
        row.ca_pem = str(payload.get("caPem") or "").strip() or None
    if "datacenterFilter" in payload:
        row.datacenter_filter = str(payload.get("datacenterFilter") or "").strip() or None
    if "folderFilter" in payload:
        row.folder_filter = str(payload.get("folderFilter") or "").strip() or None
    if "isActive" in payload:
        row.is_active = bool(payload.get("isActive"))
    password = payload.get("password")
    if password:
        row.password_cipher = encrypt_secret(str(password))
    if not row.password_cipher:
        raise ValueError("password is required.")


def create_connection(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = VSphereConnection()
    _apply_payload(row, payload)
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def update_connection(connection_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_connection(connection_id)
    _apply_payload(row, payload)
    db.session.commit()
    _inventory_cache.invalidate(f"inv:{connection_id}")
    return serialize(row)


def delete_connection(connection_id: int) -> None:
    from ..models import ClusterBuild

    row = get_connection(connection_id)
    in_use = ClusterBuild.query.filter_by(vsphere_connection_id=connection_id).count()
    if in_use:
        raise ValueError("Connection is referenced by cluster builds; remove those first.")
    db.session.delete(row)
    db.session.commit()
    _inventory_cache.invalidate(f"inv:{connection_id}")


def _config_for(row: VSphereConnection) -> VSphereConfig:
    return VSphereConfig(
        base_url=row.base_url,
        username=row.username,
        password=decrypt_secret(row.password_cipher or ""),
        skip_tls_verify=row.skip_tls_verify,
        ca_pem=row.ca_pem or "",
    )


def _record_test(row: VSphereConnection, status: str, message: str) -> None:
    row.last_connection_status = status
    row.last_connection_error = message or None
    row.last_tested_at = datetime.now(timezone.utc)
    db.session.commit()


def test_connection(connection_id: int) -> Dict[str, Any]:
    row = get_connection(connection_id)
    try:
        result = vsphere_client.test_connection(_config_for(row))
    except VSphereError as exc:
        _record_test(row, "failed", str(exc))
        return {"status": "failed", "error": str(exc)}
    _record_test(row, "ok", "")
    return result


def get_inventory(connection_id: int, *, force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Flattened VM inventory for the picker (cached ~60s, stale-servable)."""
    row = get_connection(connection_id)
    if not row.is_active:
        raise ValueError("This vSphere connection is deactivated.")
    cfg = _config_for(row)
    fetch = _inventory_fetcher or vsphere_client.full_inventory
    key = f"inv:{connection_id}"
    if force_refresh:
        _inventory_cache.invalidate(key)
    return _inventory_cache.get_or_compute(
        key,
        _INVENTORY_TTL_SECONDS,
        lambda: fetch(cfg),
        stale_ttl=_INVENTORY_STALE_TTL_SECONDS,
    )
