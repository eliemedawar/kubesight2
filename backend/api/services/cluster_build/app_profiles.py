"""App profiles: the applications a ticket may ask for.

An administrator saves, under a name, everything a Helm install needs — an
Inventory chart and version, where its values start, the whole values.yaml
and the namespace and release. Hermes picks profiles by name from a ticket
(with new VMs and Kubernetes, or onto a cluster the Cluster Builder built) and
never writes values itself. The values are kept encrypted and shown masked,
exactly like a build's charts (see ``apps``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import func

from ...db import db
from ...models import AppProfile
from ...secret_encryption import encrypt_secret
from . import apps as build_apps


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _overlay(row: AppProfile) -> Dict[str, Any]:
    return build_apps._stored_overlay(row)  # same encrypted shape as a build's chart


def serialize(row: AppProfile, *, with_values: bool = True) -> Dict[str, Any]:
    data = {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "enabled": bool(row.enabled),
        "chartSlug": row.chart_slug,
        "chartName": row.chart_name or row.chart_slug,
        "chartVersion": row.chart_version,
        "valuesFile": row.values_file or "",
        "namespace": row.namespace,
        "releaseName": row.release_name,
        "hasValues": bool(row.values_cipher),
        "createdBy": row.created_by,
        "updatedAt": _iso(row.updated_at),
    }
    if with_values:
        data["valuesYaml"] = build_apps._dump(build_apps.mask_values(_overlay(row)))
    return data


def list_profiles() -> List[Dict[str, Any]]:
    return [serialize(row) for row in AppProfile.query.order_by(AppProfile.name.asc()).all()]


def get(profile_id: int) -> AppProfile:
    row = db.session.get(AppProfile, profile_id)
    if row is None:
        raise LookupError("App profile not found.")
    return row


def _apply(row: AppProfile, payload: Dict[str, Any]) -> None:
    name = str(payload.get("name", row.name or "")).strip()
    if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError("name is required (at most 120 printable characters).")
    clash = AppProfile.query.filter(func.lower(AppProfile.name) == name.lower()).first()
    if clash is not None and clash.id != row.id:
        raise ValueError(f"An app profile called {clash.name} already exists.")
    row.name = name
    if "description" in payload:
        row.description = str(payload.get("description") or "").strip()[:2000] or None
    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))

    merged = {
        "chartSlug": payload.get("chartSlug", row.chart_slug),
        # A profile pins the version it was made with: a new chart upload must
        # not change what a ticket installs without an administrator saying so.
        "chartVersion": payload.get("chartVersion", row.chart_version),
        "valuesFile": payload.get("valuesFile", row.values_file),
        "namespace": payload.get("namespace", row.namespace),
        "releaseName": payload.get("releaseName", row.release_name),
    }
    if "valuesYaml" in payload:
        merged["valuesYaml"] = payload.get("valuesYaml")
    try:
        item = build_apps._normalize_item(merged, row if row.id else None)
    except build_apps.AppsError as exc:
        raise ValueError(str(exc)) from exc
    row.chart_slug = item["slug"]
    row.chart_name = item["name"]
    row.chart_version = item["version"]
    row.values_file = item["valuesFile"]
    row.namespace = item["namespace"]
    row.release_name = item["release"]
    if item["overlay"] is not None:
        row.values_cipher = encrypt_secret(build_apps._dump(item["overlay"])) if item["overlay"] else None


def create(payload: Dict[str, Any], *, created_by: str = "") -> Dict[str, Any]:
    row = AppProfile(created_by=created_by or None, enabled=True)
    _apply(row, payload)
    db.session.add(row)
    db.session.commit()
    return serialize(row)


def update(profile_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get(profile_id)
    _apply(row, payload)
    db.session.commit()
    return serialize(row)


def delete(profile_id: int) -> Dict[str, Any]:
    row = get(profile_id)
    summary = {"name": row.name, "chart": row.chart_slug, "release": row.release_name}
    db.session.delete(row)
    db.session.commit()
    return summary


# ---------------------------------------------------------------------------
# What Hermes sees and asks for
# ---------------------------------------------------------------------------

def catalog() -> List[Dict[str, Any]]:
    """Enabled profiles, in Hermes' words. No values: Hermes never sees them."""
    return [
        {
            "profile": row.name,
            "description": row.description or "",
            "chart": f"{row.chart_name or row.chart_slug} {row.chart_version}".strip(),
            "installsAs": f"release {row.release_name} in namespace {row.namespace}",
        }
        for row in AppProfile.query.filter_by(enabled=True).order_by(AppProfile.name.asc()).all()
    ]


def named(names: Any) -> List[AppProfile]:
    """The enabled profiles a request names, in its order. Raises ValueError."""
    if names in (None, "", []):
        return []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("apps must be a list of app profile names.")
    if len(names) > build_apps.MAX_APPS:
        raise ValueError(f"At most {build_apps.MAX_APPS} apps per request.")
    rows = []
    for name in names:
        wanted = name.strip()
        row = AppProfile.query.filter(func.lower(AppProfile.name) == wanted.lower()).first()
        if row is None or not row.enabled:
            offered = [p["profile"] for p in catalog()]
            raise ValueError(
                f"There is no app profile called {wanted!r}. Copy one exactly from appProfiles: "
                + (", ".join(offered) if offered else "none are set up.")
            )
        if row in rows:
            raise ValueError(f"App profile {row.name} is named twice.")
        rows.append(row)
    seen = set()
    for row in rows:
        key = (row.namespace, row.release_name)
        if key in seen:
            raise ValueError(
                f"Two of these app profiles install release {row.release_name} in namespace {row.namespace}."
            )
        seen.add(key)
    return rows


def describe(rows: List[AppProfile]) -> List[Dict[str, Any]]:
    """What an approver is shown: no values, only which keys they set."""
    out = []
    for row in rows:
        overlay = _overlay(row)
        out.append({
            "profileId": row.id,
            "profile": row.name,
            "chart": row.chart_name or row.chart_slug,
            "version": row.chart_version,
            "valuesFile": row.values_file or "",
            "namespace": row.namespace,
            "release": row.release_name,
            "valueKeys": sorted(str(key) for key in overlay)[:20],
        })
    return out


def items_for(rows: List[AppProfile]) -> List[Dict[str, Any]]:
    """Build-chart items for these profiles (values decrypted in memory only;
    the build stores them encrypted again)."""
    return [
        {
            "chartSlug": row.chart_slug,
            "chartVersion": row.chart_version,
            "valuesFile": row.values_file or "",
            "namespace": row.namespace,
            "releaseName": row.release_name,
            "valuesYaml": build_apps.values_yaml_of(row),
        }
        for row in rows
    ]
