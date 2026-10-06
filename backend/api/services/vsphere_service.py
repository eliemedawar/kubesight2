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
        # The separate account OpenTofu uses to create and delete VMs.
        "provisioningUsername": row.provisioning_username,
        "provisioningPasswordConfigured": bool(row.provisioning_password_cipher),
        "provisioningConfigured": bool(
            row.provisioning_username and row.provisioning_password_cipher
        ),
        "provisioningLastTestAt": _iso(row.provisioning_last_test_at),
        "provisioningLastTestStatus": row.provisioning_last_test_status,
        "provisioningLastTestMessage": row.provisioning_last_test_message,
        "provisioningPrivileges": row.provisioning_privileges_json or [],
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
    if "provisioningUsername" in payload:
        prov_user = str(payload.get("provisioningUsername") or "").strip()
        if not prov_user:
            # Clearing the user removes the account entirely: a password with
            # nobody to log in as is a secret kept for no reason.
            row.provisioning_username = None
            row.provisioning_password_cipher = None
            row.provisioning_last_test_status = None
            row.provisioning_last_test_message = None
            row.provisioning_privileges_json = None
        else:
            if prov_user.lower() == username.lower():
                raise ValueError(
                    "Use a different account for provisioning than for browsing, "
                    "so browsing never needs more than the Read-Only role."
                )
            row.provisioning_username = prov_user[:255]
    prov_password = payload.get("provisioningPassword")
    if prov_password:
        if not row.provisioning_username:
            raise ValueError("Give the provisioning account's username with its password.")
        row.provisioning_password_cipher = encrypt_secret(str(prov_password))


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
    from ..models import VSphereIpReservation, VSphereNetworkRange

    ranges = VSphereNetworkRange.query.filter_by(connection_id=connection_id).all()
    if any(VSphereIpReservation.query.filter_by(range_id=r.id).count() for r in ranges):
        raise ValueError("Builds still hold addresses from this vCenter's networks.")
    for network_range in ranges:
        db.session.delete(network_range)
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


def provisioning_config(row: VSphereConnection) -> VSphereConfig:
    """The account OpenTofu uses. Never falls back to the browsing account."""
    if not (row.provisioning_username and row.provisioning_password_cipher):
        raise ValueError(
            f"{row.name} has no provisioning account. An administrator adds one "
            "under Cluster Builder → Sources → vCenter."
        )
    return VSphereConfig(
        base_url=row.base_url,
        username=row.provisioning_username,
        password=decrypt_secret(row.provisioning_password_cipher or ""),
        skip_tls_verify=row.skip_tls_verify,
        ca_pem=row.ca_pem or "",
    )


def placement(connection_id: int, *, force_refresh: bool = False) -> Dict[str, Any]:
    """Datacenters, clusters, folders, datastores, networks and VM templates.

    Read with the provisioning account when there is one — that is the account
    the VMs will be created with, so it sees exactly what it can use.
    """
    from .cluster_build.provisioning import inventory

    row = get_connection(connection_id)
    if not row.is_active:
        raise ValueError("This vSphere connection is deactivated.")
    try:
        cfg = provisioning_config(row)
    except ValueError:
        cfg = _config_for(row)
    return inventory.placement(cfg, cache_key=f"prov:{connection_id}", force_refresh=force_refresh)


def test_provisioning(connection_id: int) -> Dict[str, Any]:
    """Log in with the provisioning account and check its privileges.

    Checked on the first datacenter (or the connection's datacenter filter).
    A role granted only on a narrower folder can read as missing here; the
    plan re-checks on the exact folder, pool and datastore a build uses.
    """
    from .cluster_build.provisioning import inventory

    row = get_connection(connection_id)
    cfg = provisioning_config(row)
    now = datetime.now(timezone.utc)
    try:
        found = inventory.placement(cfg, cache_key=f"prov:{connection_id}", force_refresh=True)
        datacenters = found.get("datacenters") or []
        if row.datacenter_filter:
            datacenters = [dc for dc in datacenters if dc["name"] == row.datacenter_filter] or datacenters
        if not datacenters:
            raise VSphereError("The provisioning account sees no datacenter.")
        dc = datacenters[0]
        privileges = inventory.check_privileges(cfg, {"datacenter": dc["id"]})
    except VSphereError as exc:
        row.provisioning_last_test_at = now
        row.provisioning_last_test_status = "failed"
        row.provisioning_last_test_message = str(exc)[:1000]
        db.session.commit()
        return {"status": "failed", "error": str(exc)}
    missing = [p for p in privileges if not p.get("granted")]
    for item in privileges:
        item["entityName"] = dc["name"]
    row.provisioning_last_test_at = now
    row.provisioning_last_test_status = "ok" if not missing else "warn"
    row.provisioning_last_test_message = (
        f"All {len(privileges)} privileges granted on {dc['name']}." if not missing else
        f"{len(missing)} of {len(privileges)} privileges not granted on {dc['name']}: "
        + ", ".join(p["privilege"] for p in missing[:6])
    )
    row.provisioning_privileges_json = privileges
    db.session.commit()
    return {
        "status": row.provisioning_last_test_status,
        "message": row.provisioning_last_test_message,
        "privileges": privileges,
        "datacenter": dc["name"],
    }


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
