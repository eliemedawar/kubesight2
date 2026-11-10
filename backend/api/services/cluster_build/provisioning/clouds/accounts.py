"""Cloud accounts: the keys KubeSight signs in to Oracle, AWS and Google with.

Secrets are stored as one Fernet-encrypted JSON object and never leave this
module except to sign a request or to hand to OpenTofu's provider in a job's
environment. Editing an account never shows them; a blank secret on the form
keeps the stored one.

Inventory reads (regions, networks, images…) are cached briefly per account and
region: the wizard asks for the same lists several times while someone clicks
through it, and a cloud's API is not free to call.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .....db import db
from .....models import ClusterBuild, CloudAccount
from .....secret_encryption import decrypt_secret, encrypt_secret
from .. import inventory as vsphere_inventory
from . import PROVIDERS, adapter
from .base import Account, CloudError

_INVENTORY_TTL_S = 120
_cache_lock = threading.Lock()
_cache: Dict[Tuple[int, str, str], Tuple[float, Any]] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def simulated() -> bool:
    """Demo / test mode: no cloud is called, every adapter answers from its
    simulated inventory (the same switch as simulated OpenTofu)."""
    return vsphere_inventory.simulation_enabled()


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def get_row(account_id: int) -> CloudAccount:
    row = db.session.get(CloudAccount, int(account_id))
    if row is None:
        raise LookupError("Cloud account not found.")
    return row


def _secrets(row: CloudAccount) -> Dict[str, str]:
    if not row.secrets_cipher:
        return {}
    try:
        return json.loads(decrypt_secret(row.secrets_cipher) or "{}")
    except (ValueError, TypeError):
        return {}


def view(row: CloudAccount) -> Account:
    """The account with its secrets decrypted — for one request or one job."""
    return Account(
        id=row.id, provider=row.provider, name=row.name, region=row.region or "",
        settings=dict(row.settings_json or {}), secrets=_secrets(row), ca_pem=row.ca_pem or "",
    )


def builds_using(account_id: int) -> int:
    return ClusterBuild.query.filter(
        ClusterBuild.cloud_account_id == int(account_id),
        ClusterBuild.status != "destroyed",
    ).count()


def serialize(row: CloudAccount, *, in_use: Optional[int] = None) -> Dict[str, Any]:
    module = adapter(row.provider)
    secrets = _secrets(row)
    try:
        identity = module.identity_label(view(row))
    except Exception:  # noqa: BLE001 — a label, never worth failing a list over
        identity = ""
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "providerLabel": module.LABEL,
        "managedLabel": module.MANAGED_LABEL,
        "region": row.region or "",
        "settings": dict(row.settings_json or {}),
        "secretsSet": {key: True for key, value in secrets.items() if value},
        "hasCaPem": bool(row.ca_pem),
        "identity": identity,
        "lastTestAt": _iso(row.last_test_at),
        "lastTestStatus": row.last_test_status,
        "lastTestMessage": row.last_test_message,
        "createdBy": row.created_by,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "inUse": builds_using(row.id) if in_use is None else in_use,
    }


def list_accounts() -> List[Dict[str, Any]]:
    rows = CloudAccount.query.order_by(CloudAccount.provider, CloudAccount.name).all()
    return [serialize(row) for row in rows]


def _apply(row: CloudAccount, payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", row.name or "") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("Give the account a name (at most 120 characters).")
    clash = CloudAccount.query.filter(CloudAccount.name == name, CloudAccount.id != row.id).first()
    if clash is not None:
        raise ValueError(f"Another cloud account is already called {name}.")
    row.name = name
    provider = str(payload.get("provider", row.provider or "") or "").strip()
    if provider not in PROVIDERS:
        raise ValueError("provider must be oci, aws or gcp.")
    if row.id and row.provider and provider != row.provider:
        raise ValueError("An account cannot change cloud. Add a new account instead.")
    row.provider = provider
    module = adapter(provider)
    region = str(payload.get("region", row.region or "") or "").strip()
    if not region or len(region) > 64:
        raise ValueError("Choose the account's default region.")
    row.region = region
    raw_settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    raw_secrets = payload.get("secrets") if isinstance(payload.get("secrets"), dict) else {}
    settings, secrets = module.normalize_account(
        raw_settings, raw_secrets, dict(row.settings_json or {}), _secrets(row),
    )
    row.settings_json = settings
    row.secrets_cipher = encrypt_secret(json.dumps(secrets)) if secrets else None
    if "caPem" in payload:
        ca_pem = str(payload.get("caPem") or "").strip()
        if ca_pem and "BEGIN CERTIFICATE" not in ca_pem:
            raise ValueError("The CA must be one or more PEM certificates.")
        row.ca_pem = (ca_pem + "\n") if ca_pem else None


def create(payload: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
    row = CloudAccount(created_by=actor or None)
    _apply(row, payload or {})
    db.session.add(row)
    db.session.commit()
    _forget(row.id)
    return serialize(row, in_use=0)


def update(account_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_row(account_id)
    _apply(row, payload or {})
    # New keys: the last test said nothing about them.
    row.last_test_at = None
    row.last_test_status = None
    row.last_test_message = None
    db.session.commit()
    _forget(row.id)
    return serialize(row)


def delete(account_id: int) -> None:
    row = get_row(account_id)
    used = builds_using(row.id)
    if used:
        raise ValueError(
            f"{used} build{'s' if used != 1 else ''} still use{'' if used != 1 else 's'} this account. "
            "Destroy or delete them first: their cloud resources are deleted with its keys."
        )
    db.session.delete(row)
    db.session.commit()
    _forget(row.id)


def test(account_id: int) -> Dict[str, Any]:
    row = get_row(account_id)
    module = adapter(row.provider)
    try:
        if simulated():
            ok, message = True, "Demo mode: no cloud was called, so the keys were not checked."
        else:
            ok, message = True, f"Signed in as {module.test(view(row))}."
    except CloudError as exc:
        ok, message = False, str(exc)
    except Exception as exc:  # noqa: BLE001 — a malformed key raises all sorts
        ok, message = False, f"{type(exc).__name__}: {exc}"
    row.last_test_at = _utcnow()
    row.last_test_status = "ok" if ok else "failed"
    row.last_test_message = message[:2000]
    db.session.commit()
    return {"ok": ok, "message": message, "account": serialize(row)}


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _forget(account_id: int) -> None:
    with _cache_lock:
        for key in [k for k in _cache if k[0] == account_id]:
            _cache.pop(key, None)


def _cached(key: Tuple[int, str, str], refresh: bool, load):
    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < _INVENTORY_TTL_S:
            return hit[1]
    value = load()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def regions(account_id: int, *, refresh: bool = False) -> List[Dict[str, Any]]:
    row = get_row(account_id)
    module = adapter(row.provider)
    if simulated():
        if row.provider == "oci" and (row.settings_json or {}).get("authMode") == "workload_identity":
            return [{"id": row.region, "name": row.region}]
        return module.simulated_regions()
    return _cached((row.id, "regions", ""), refresh, lambda: module.regions(view(row)))


def inventory(account_id: int, region: str, scope: Optional[Dict[str, Any]] = None,
              *, refresh: bool = False) -> Dict[str, Any]:
    row = get_row(account_id)
    module = adapter(row.provider)
    region = (region or row.region or "").strip()
    if not region:
        raise ValueError("Choose a region.")
    scope = {k: v for k, v in (scope or {}).items() if v}
    if simulated():
        if row.provider == "oci" and (row.settings_json or {}).get("authMode") == "workload_identity":
            from . import oci_wi
            return oci_wi.simulated_inventory(view(row), region)
        return module.simulated_inventory(region, scope)
    key = (row.id, region, json.dumps(scope, sort_keys=True))
    return _cached(key, refresh, lambda: module.inventory(view(row), region, scope))


def machine_catalog(row: CloudAccount, region: str, scope: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    try:
        return list(inventory(row.id, region, scope).get("machineTypes") or [])
    except (CloudError, ValueError):
        return []


def resolve_sizes(account_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The machine type each role's CPU and memory come out as on this cloud."""
    row = get_row(account_id)
    module = adapter(row.provider)
    spec = payload.get("provisioning") if isinstance(payload.get("provisioning"), dict) else {}
    region = str(payload.get("region") or spec.get("region") or row.region or "")
    catalog = machine_catalog(row, region, {"compartmentId": spec.get("compartmentId")})
    out: Dict[str, Any] = {}
    for role, size in (payload.get("sizes") or {}).items():
        if not isinstance(size, dict):
            continue
        try:
            wanted = {"cpu": int(size.get("cpu") or 0), "memoryGb": float(size.get("memoryGb") or 0)}
            out[role] = module.resolve_machine_type(
                {**wanted, "role": role}, spec, catalog,
            )
        except (ValueError, TypeError) as exc:
            out[role] = {"error": str(exc)}
    return {"sizes": out}
