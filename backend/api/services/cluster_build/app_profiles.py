"""App profiles: the applications a ticket may ask for.

An administrator saves, under a name, everything a Helm install needs — an
Inventory chart and version, where its values start, the whole values.yaml
and the namespace and release. Hermes picks profiles by name from a ticket
(with new VMs and Kubernetes, or onto a cluster the Cluster Builder built) and
never writes values itself. The values are kept encrypted and shown masked,
exactly like a build's charts (see ``apps``).

A profile may also let a ticket name another of the chart's values-*.yaml
("with the UAT values"). The administrator's values.yaml usually starts as a
full copy of the chart's values, so only what the administrator changed in it
(the passwords, typically) goes on top of the file the ticket picks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        "allowedValuesFiles": other_files(row),
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

    asked = payload.get("allowedValuesFiles", row.values_files_json or [])
    if not isinstance(asked, list) or not all(isinstance(name, str) for name in asked):
        raise ValueError("allowedValuesFiles must be a list of the chart's values file names.")
    charts = build_apps._charts()
    files: List[str] = []
    for name in (name.strip() for name in asked):
        if not name or name == row.values_file or name in files:
            continue
        try:
            charts.resolve_chart(row.chart_slug, row.chart_version, name)
        except charts.ChartTemplateError as exc:
            raise ValueError(str(exc)) from exc
        files.append(name)
    row.values_files_json = files or None


def other_files(row: AppProfile) -> List[str]:
    """The values files a ticket may name instead of the profile's own."""
    return [
        name for name in (row.values_files_json or [])
        if isinstance(name, str) and name and name != (row.values_file or "")
    ]


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
    out = []
    for row in AppProfile.query.filter_by(enabled=True).order_by(AppProfile.name.asc()).all():
        entry = {
            "profile": row.name,
            "description": row.description or "",
            "chart": f"{row.chart_name or row.chart_slug} {row.chart_version}".strip(),
            "installsAs": f"release {row.release_name} in namespace {row.namespace}",
            "valuesFile": row.values_file or "chart defaults",
        }
        if other_files(row):
            entry["ticketMayPick"] = other_files(row)
        out.append(entry)
    return out


@dataclass(frozen=True)
class Pick:
    """A profile as a request asked for it, with the values file it installs from."""

    row: AppProfile
    values_file: str

    @property
    def picked(self) -> bool:
        return self.values_file != (self.row.values_file or "")


def _file_keys(name: str) -> set:
    base = name.rsplit("/", 1)[-1]
    stem = re.sub(r"\.ya?ml$", "", base, flags=re.IGNORECASE)
    short = re.sub(r"^values[-_.]", "", stem, flags=re.IGNORECASE)
    return {name.lower(), base.lower(), stem.lower(), short.lower()}


def _file_named(row: AppProfile, asked: str) -> str:
    """The values file a ticket names: exactly, or as "values-uat" or "uat"."""
    wanted = asked.strip().lower()
    choices = [name for name in [row.values_file or ""] + other_files(row) if name]
    hits = [name for name in choices if wanted in _file_keys(name)]
    if len(hits) == 1:
        return hits[0]
    if other_files(row):
        raise ValueError(
            f"App profile {row.name} has no values file {asked!r} a ticket may pick. "
            f"It allows: {', '.join(choices)}."
        )
    raise ValueError(
        f"App profile {row.name} always installs with {row.values_file or 'the chart defaults'}; "
        f"a ticket cannot pick {asked!r}."
    )


def named(entries: Any) -> List[Pick]:
    """The enabled profiles a request names, in its order. Raises ValueError.

    Each entry is a profile name, or ``{"profile": name, "valuesFile": file}``
    for one of the files the profile lets a ticket pick.
    """
    if entries in (None, "", []):
        return []
    if not isinstance(entries, list):
        raise ValueError("apps must be a list of app profile names.")
    if len(entries) > build_apps.MAX_APPS:
        raise ValueError(f"At most {build_apps.MAX_APPS} apps per request.")
    picks: List[Pick] = []
    for entry in entries:
        if isinstance(entry, dict):
            name, asked_file = entry.get("profile"), entry.get("valuesFile")
        else:
            name, asked_file = entry, None
        if not isinstance(name, str) or (asked_file is not None and not isinstance(asked_file, str)):
            raise ValueError('Each app is a profile name, or {"profile": name, "valuesFile": file}.')
        wanted = name.strip()
        row = AppProfile.query.filter(func.lower(AppProfile.name) == wanted.lower()).first()
        if row is None or not row.enabled:
            offered = [p["profile"] for p in catalog()]
            raise ValueError(
                f"There is no app profile called {wanted!r}. Copy one exactly from appProfiles: "
                + (", ".join(offered) if offered else "none are set up.")
            )
        if any(pick.row is row for pick in picks):
            raise ValueError(f"App profile {row.name} is named twice.")
        if (asked_file or "").strip():
            values_file = _file_named(row, asked_file)
        else:
            values_file = row.values_file or ""
        picks.append(Pick(row, values_file))
    seen = set()
    for pick in picks:
        key = (pick.row.namespace, pick.row.release_name)
        if key in seen:
            raise ValueError(
                f"Two of these app profiles install release {pick.row.release_name} "
                f"in namespace {pick.row.namespace}."
            )
        seen.add(key)
    return picks


def picks_from(entries: List[Dict[str, Any]]) -> List[Pick]:
    """The picks a request saved (``describe`` output), checked again. Raises LookupError."""
    picks = []
    for entry in entries or []:
        row = db.session.get(AppProfile, entry.get("profileId") or 0)
        if row is None or not row.enabled:
            raise LookupError(f"App profile {entry.get('profile')} was deleted or switched off.")
        values_file = entry.get("valuesFile") or ""
        if values_file not in [row.values_file or ""] + other_files(row):
            raise LookupError(f"App profile {row.name} no longer allows {values_file}.")
        picks.append(Pick(row, values_file))
    return picks


def _changed(values: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """The keys of ``values`` that differ from ``base`` (maps compared key by key)."""
    out: Dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            nested = _changed(value, base[key])
            if nested:
                out[key] = nested
        elif key not in base or base[key] != value:
            out[key] = value
    return out


def _edits(row: AppProfile) -> Dict[str, Any]:
    """What the administrator changed against where the profile's values start."""
    start = build_apps._charts().starting_values(row.chart_slug, row.chart_version, row.values_file or "")
    return _changed(_overlay(row), start)


def describe(picks: List[Pick]) -> List[Dict[str, Any]]:
    """What an approver is shown: no values, only which keys they set."""
    out = []
    for pick in picks:
        row = pick.row
        overlay = _edits(row) if pick.picked else _overlay(row)
        out.append({
            "profileId": row.id,
            "profile": row.name,
            "chart": row.chart_name or row.chart_slug,
            "version": row.chart_version,
            "valuesFile": pick.values_file,
            "valuesFilePicked": pick.picked,
            "namespace": row.namespace,
            "release": row.release_name,
            "valueKeys": sorted(str(key) for key in overlay)[:20],
        })
    return out


def items_for(picks: List[Pick]) -> List[Dict[str, Any]]:
    """Build-chart items for these picks (values decrypted in memory only;
    the build stores them encrypted again)."""
    return [
        {
            "chartSlug": pick.row.chart_slug,
            "chartVersion": pick.row.chart_version,
            "valuesFile": pick.values_file,
            "namespace": pick.row.namespace,
            "releaseName": pick.row.release_name,
            "valuesYaml": (
                build_apps._dump(_edits(pick.row)) if pick.picked else build_apps.values_yaml_of(pick.row)
            ),
        }
        for pick in picks
    ]
