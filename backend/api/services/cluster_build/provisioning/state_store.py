"""OpenTofu state and its lock, one per build, encrypted in KubeSight's database.

The ``tofu`` process reaches this through the HTTP state backend route
(``routes/cluster_provisioning.py``); the simulated engine calls it directly.
Either way the rules are the same as OpenTofu's own:

  * a lock is held by one lock id at a time, and a second LOCK gets the
    holder's lock info back with 423;
  * a state write while locked must carry the holder's lock id;
  * the job that took a lock is recorded, so restart recovery can release a
    lock on behalf of a dead job — and only that job's lock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ....db import db
from ....models import ClusterInfraState
from ....secret_encryption import decrypt_secret, encrypt_secret


class StateLocked(Exception):
    def __init__(self, lock_info: Optional[Dict[str, Any]]):
        super().__init__("State is locked by another operation.")
        self.lock_info = lock_info or {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row(build_id: int, *, create: bool = False, for_update: bool = False) -> Optional[ClusterInfraState]:
    query = ClusterInfraState.query.filter_by(build_id=build_id)
    if for_update:
        query = query.with_for_update()
    row = query.first()
    if row is None and create:
        row = ClusterInfraState(build_id=build_id, version=0, resource_count=0)
        db.session.add(row)
        db.session.flush()
    return row


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def read_state(build_id: int) -> Optional[str]:
    row = _row(build_id)
    if row is None or not row.state_cipher:
        return None
    return decrypt_secret(row.state_cipher)


def read_state_document(build_id: int) -> Optional[Dict[str, Any]]:
    text = read_state(build_id)
    if not text:
        return None
    try:
        document = json.loads(text)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _count_resources(document: Dict[str, Any]) -> int:
    count = 0
    for resource in document.get("resources") or []:
        if resource.get("mode") != "managed":
            continue
        count += len(resource.get("instances") or [])
    return count


def _count_vms(document: Dict[str, Any]) -> int:
    """How many VMs ``vm_instances`` would list for this state document."""
    names = set()
    for resource in document.get("resources") or []:
        if resource.get("mode") != "managed" or resource.get("type") != "vsphere_virtual_machine":
            continue
        for instance in resource.get("instances") or []:
            if instance.get("index_key") is not None:
                names.add(str(instance.get("index_key")))
    return len(names)


def write_state(build_id: int, text: str, *, lock_id: Optional[str] = None) -> ClusterInfraState:
    """Store a state document OpenTofu sent. Refused while someone else holds the lock."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ValueError("State is not valid JSON.") from exc
    if not isinstance(document, dict):
        raise ValueError("State must be a JSON object.")
    row = _row(build_id, create=True, for_update=True)
    if row.lock_id and lock_id != row.lock_id:
        db.session.rollback()
        raise StateLocked(row.lock_info_json)
    row.state_cipher = encrypt_secret(text)
    row.serial = document.get("serial") if isinstance(document.get("serial"), int) else row.serial
    row.lineage = str(document.get("lineage") or row.lineage or "")[:64] or None
    row.resource_count = _count_resources(document)
    row.vm_count = _count_vms(document)
    row.version = (row.version or 0) + 1
    row.updated_at = _utcnow()
    db.session.commit()
    return row


def delete_state(build_id: int, *, lock_id: Optional[str] = None) -> None:
    row = _row(build_id, for_update=True)
    if row is None:
        return
    if row.lock_id and lock_id != row.lock_id:
        db.session.rollback()
        raise StateLocked(row.lock_info_json)
    row.state_cipher = None
    row.resource_count = 0
    row.vm_count = 0
    row.version = (row.version or 0) + 1
    db.session.commit()


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def lock(build_id: int, info: Dict[str, Any], *, job_id: Optional[int]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    lock_id = str((info or {}).get("ID") or "").strip()
    if not lock_id:
        raise ValueError("Lock request carries no ID.")
    row = _row(build_id, create=True, for_update=True)
    if row.lock_id and row.lock_id != lock_id:
        held = row.lock_info_json
        db.session.rollback()
        return False, held
    row.lock_id = lock_id[:64]
    row.lock_info_json = info
    row.lock_job_id = job_id
    row.locked_at = _utcnow()
    db.session.commit()
    return True, None


def unlock(build_id: int, info: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    row = _row(build_id, for_update=True)
    if row is None or not row.lock_id:
        db.session.rollback()
        return True, None
    lock_id = str((info or {}).get("ID") or "").strip()
    if lock_id and lock_id != row.lock_id:
        held = row.lock_info_json
        db.session.rollback()
        return False, held
    _clear_lock(row)
    db.session.commit()
    return True, None


def _clear_lock(row: ClusterInfraState) -> None:
    row.lock_id = None
    row.lock_info_json = None
    row.lock_job_id = None
    row.locked_at = None


def release_job_lock(job_id: int) -> bool:
    """Drop a lock a dead job left behind. Never touches anyone else's lock."""
    rows = ClusterInfraState.query.filter_by(lock_job_id=job_id).all()
    for row in rows:
        _clear_lock(row)
    db.session.commit()
    return bool(rows)


def force_release(build_id: int) -> Optional[Dict[str, Any]]:
    """Administrator escape hatch for a lock nobody holds any more."""
    row = _row(build_id, for_update=True)
    if row is None or not row.lock_id:
        db.session.rollback()
        return None
    released = {"lockId": row.lock_id, "jobId": row.lock_job_id, "info": row.lock_info_json}
    _clear_lock(row)
    db.session.commit()
    return released


# ---------------------------------------------------------------------------
# Reading what the state says exists
# ---------------------------------------------------------------------------

def managed_resources(build_id: int) -> List[Dict[str, Any]]:
    document = read_state_document(build_id) or {}
    out: List[Dict[str, Any]] = []
    for resource in document.get("resources") or []:
        if resource.get("mode") != "managed":
            continue
        base = f"{resource.get('type')}.{resource.get('name')}"
        for instance in resource.get("instances") or []:
            key = instance.get("index_key")
            address = base if key is None else (
                f'{base}["{key}"]' if isinstance(key, str) else f"{base}[{key}]"
            )
            out.append({
                "address": address,
                "type": resource.get("type"),
                "name": resource.get("name"),
                "key": key,
                "attributes": instance.get("attributes") or {},
            })
    return out


def vm_instances(build_id: int) -> Dict[str, Dict[str, Any]]:
    """The VMs the state records, keyed by VM name."""
    return {
        str(item["key"]): item["attributes"]
        for item in managed_resources(build_id)
        if item["type"] == "vsphere_virtual_machine" and item["key"] is not None
    }


# What the read paths need from a state row: everything but the ciphertext,
# plus whether there is one. Build pages poll these every few seconds.
_LIGHT_COLUMNS = (
    ClusterInfraState.build_id,
    ClusterInfraState.version,
    ClusterInfraState.serial,
    ClusterInfraState.resource_count,
    ClusterInfraState.vm_count,
    ClusterInfraState.lock_id,
    ClusterInfraState.lock_job_id,
    ClusterInfraState.locked_at,
    ClusterInfraState.lock_info_json,
    ClusterInfraState.updated_at,
    ClusterInfraState.state_cipher.isnot(None).label("has_state"),
)


def light_rows(build_ids) -> Dict[int, Any]:
    """State rows without their ciphertext, keyed by build id, in one query."""
    ids = list({int(b) for b in build_ids})
    if not ids:
        return {}
    rows = db.session.query(*_LIGHT_COLUMNS).filter(ClusterInfraState.build_id.in_(ids)).all()
    return {row.build_id: row for row in rows}


def _light_row(build_id: int):
    return db.session.query(*_LIGHT_COLUMNS).filter(
        ClusterInfraState.build_id == build_id
    ).first()


def vm_count_of(build_id: int, row=None) -> int:
    """VMs the state lists, from the stored count. A row written before the
    count was stored is decrypted once and the count kept for next time."""
    row = row if row is not None else _light_row(build_id)
    if row is None or not row.has_state:
        return 0
    if row.vm_count is not None:
        return row.vm_count
    count = len(vm_instances(build_id))
    try:
        ClusterInfraState.query.filter(
            ClusterInfraState.build_id == build_id,
            ClusterInfraState.vm_count.is_(None),
        ).update({"vm_count": count}, synchronize_session=False)
    except Exception:  # noqa: BLE001 — the count is a cache; reading must not fail over it
        pass
    return count


def summary(build_id: int, row=None) -> Dict[str, Any]:
    row = row if row is not None else _light_row(build_id)
    if row is None:
        return {"exists": False, "version": 0, "resourceCount": 0, "vmCount": 0, "locked": False}
    return {
        "exists": bool(row.has_state),
        "version": row.version or 0,
        "serial": row.serial,
        "resourceCount": row.resource_count or 0,
        "vmCount": vm_count_of(build_id, row),
        "locked": bool(row.lock_id),
        "lockJobId": row.lock_job_id,
        "lockedAt": row.locked_at.isoformat() if row.locked_at else None,
        "lockOperation": (row.lock_info_json or {}).get("Operation"),
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def has_resources(build_id: int, row=None) -> bool:
    row = row if row is not None else _light_row(build_id)
    return bool(row and row.has_state and (row.resource_count or 0) > 0)
