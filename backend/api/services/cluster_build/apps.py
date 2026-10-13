"""Inventory Helm charts installed on the cluster a build makes.

A build carries an ordered list of charts from Inventory → Helm Charts. Each
names a chart, a version, where its values start (the chart's defaults or one
of its stored ``values-*.yaml``) and the person's whole values.yaml on top,
plus the namespace and release name. They are installed one after another by
the ``apps`` phase, after add-ons and workloads, through the same
``helm_service`` install a person runs from Inventory — the registry image
check and the App Catalog entry included.

Values usually hold passwords — import blanks them in the stored chart, so the
person types them. They are kept encrypted (``values_cipher``), shown masked
(``<hidden>`` for anything whose key looks like a credential; sending the mask
back keeps the stored value) and dropped once the chart is installed.

Approval: charts chosen with the cluster are part of building it and are
installed by the build run that creates the cluster. Charts added on day two
follow the cluster's change-approval rule — on a cluster that needs approval
they are queued as a change bundle that refers to the encrypted values by
``valuesRef`` instead of carrying them.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ...db import db
from ...models import ClusterBuild, ClusterBuildApp
from ...secret_encryption import decrypt_secret_or_none, encrypt_secret
from .scrub import scrub

MAX_APPS = 20
MAX_VALUES_BYTES = 256 * 1024
MASK = "<hidden>"
REF_PREFIX = "cluster_build_app:"
_RENDER_TIMEOUT_S = 120

# Statuses whose chart and values can still change.
EDITABLE = ("pending", "failed", "rejected")
# What the apps phase installs. A rejected chart waits for a person; one stuck
# in "installing" was interrupted (a restart) and is tried again.
TO_INSTALL = ("pending", "failed", "installing")


class AppsError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _charts():
    from .. import helm_chart_template_service

    return helm_chart_template_service


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

def _sensitive(key: str) -> bool:
    from ..helm_service import _is_sensitive_value_key

    return _is_sensitive_value_key(str(key))


def mask_values(value: Any) -> Any:
    """Credential-looking leaves replaced by ``<hidden>``."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                out[key] = mask_values(item)
            elif item not in (None, "") and _sensitive(key):
                out[key] = MASK
            else:
                out[key] = item
        return out
    if isinstance(value, list):
        return [mask_values(item) for item in value]
    return value


def _unmask(new: Any, old: Any, path: str = "") -> Any:
    """Put the stored value back wherever the person left ``<hidden>``."""
    if isinstance(new, dict):
        old_map = old if isinstance(old, dict) else {}
        return {
            key: _unmask(item, old_map.get(key), f"{path}.{key}" if path else str(key))
            for key, item in new.items()
        }
    if isinstance(new, list):
        old_list = old if isinstance(old, list) else []
        return [
            _unmask(item, old_list[index] if index < len(old_list) else None, f"{path}[{index}]")
            for index, item in enumerate(new)
        ]
    if new == MASK:
        if old is None or old == MASK:
            raise AppsError(
                f"{path or 'A value'} is {MASK} but there is no saved value for it — type the value."
            )
        return old
    return new


def _dump(values: Dict[str, Any]) -> str:
    if not values:
        return ""
    return yaml.safe_dump(values, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _stored_overlay(app: ClusterBuildApp) -> Dict[str, Any]:
    text = decrypt_secret_or_none(app.values_cipher) if app.values_cipher else None
    if not text:
        return {}
    loaded = yaml.safe_load(text)
    return loaded if isinstance(loaded, dict) else {}


def values_yaml_of(app: ClusterBuildApp) -> str:
    """The stored values.yaml, unmasked. For the install only."""
    return _dump(_stored_overlay(app))


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_app(app: ClusterBuildApp, *, with_values: bool = False) -> Dict[str, Any]:
    data = {
        "id": app.id,
        "position": app.position,
        "chartSlug": app.chart_slug,
        "chartName": app.chart_name or app.chart_slug,
        "chartVersion": app.chart_version,
        "valuesFile": app.values_file or "",
        "namespace": app.namespace,
        "releaseName": app.release_name,
        "status": app.status,
        "error": app.error,
        "bundleId": app.bundle_id,
        "profileId": app.profile_id,
        "createdBy": app.created_by,
        "installedAt": _iso(app.installed_at),
    }
    if with_values:
        # Only the detail view reads the (deferred, encrypted) values.
        data["hasValues"] = bool(app.values_cipher)
        if app.status in EDITABLE:
            data["valuesYaml"] = _dump(mask_values(_stored_overlay(app)))
    return data


def serialize_apps(build: ClusterBuild, *, with_values: bool = False) -> List[Dict[str, Any]]:
    return [serialize_app(app, with_values=with_values) for app in build.apps]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_names(namespace: str, release: str) -> Tuple[str, str]:
    from ..helm_service import validate_namespace_name, validate_release_name

    namespace = str(namespace or "").strip()
    release = str(release or "").strip().lower()
    ok, error = validate_namespace_name(namespace)
    if not ok:
        raise AppsError(f"{namespace or 'Namespace'}: {error}")
    ok, error = validate_release_name(release)
    if not ok:
        raise AppsError(f"{release or 'Release'}: {error}")
    return namespace, release


def _normalize_item(raw: Any, existing: Optional[ClusterBuildApp]) -> Dict[str, Any]:
    """One requested chart, checked. ``valuesYaml`` absent keeps what is stored."""
    if not isinstance(raw, dict):
        raise AppsError("Each app must be an object.")
    charts = _charts()
    slug = str(raw.get("chartSlug") or raw.get("chartTemplateId") or "").strip()
    if not slug:
        raise AppsError("Pick a Helm chart for every app.")
    try:
        chart = charts.resolve_chart(
            slug, str(raw.get("chartVersion") or ""), str(raw.get("valuesFile") or "")
        )
    except charts.ChartTemplateError as exc:
        raise AppsError(f"{slug}: {exc}") from exc
    namespace, release = _validate_names(
        raw.get("namespace"), raw.get("releaseName") or chart["slug"]
    )

    overlay: Optional[Dict[str, Any]] = None
    if "valuesYaml" in raw:
        text = str(raw.get("valuesYaml") or "")
        if len(text.encode("utf-8")) > MAX_VALUES_BYTES:
            raise AppsError(f"{release}: values.yaml is larger than 256 KB.")
        try:
            parsed = charts.parse_values_overlay(text)
        except charts.ChartTemplateError as exc:
            raise AppsError(f"{release}: {exc}") from exc
        overlay = _unmask(parsed, _stored_overlay(existing) if existing else {})
    return {
        "slug": chart["slug"],
        "name": chart["name"],
        "version": chart["version"],
        "valuesFile": chart["valuesFile"],
        "namespace": namespace,
        "release": release,
        "overlay": overlay,
    }


def _apply_item(app: ClusterBuildApp, item: Dict[str, Any]) -> None:
    app.chart_slug = item["slug"]
    app.chart_name = item["name"]
    app.chart_version = item["version"]
    app.values_file = item["valuesFile"]
    app.namespace = item["namespace"]
    app.release_name = item["release"]
    if item["overlay"] is not None:
        app.values_cipher = encrypt_secret(_dump(item["overlay"])) if item["overlay"] else None
    if app.status != "installed":
        app.status = "pending"
        app.error = None
        app.bundle_id = None


def _check_unique(apps: List[ClusterBuildApp]) -> None:
    seen = set()
    for app in apps:
        key = (app.namespace, app.release_name)
        if key in seen:
            raise AppsError(
                f"Release {app.release_name} appears twice in namespace {app.namespace}."
            )
        seen.add(key)


def replace_apps(build: ClusterBuild, raw_items: Any, *, actor: str = "") -> None:
    """The wizard's list: every chart not yet installed is replaced by it.

    Installed charts stay as they are, first. An item with the ``id`` of a
    chart already on the build edits it (and keeps its stored values unless
    ``valuesYaml`` is sent).
    """
    if raw_items is None:
        return
    if not isinstance(raw_items, list):
        raise AppsError("apps must be a list.")
    installed = [app for app in build.apps if app.status == "installed"]
    open_by_id = {app.id: app for app in build.apps if app.status != "installed"}
    if len(installed) + len(raw_items) > MAX_APPS:
        raise AppsError(f"A build installs at most {MAX_APPS} Helm charts.")
    if any(app.status in ("installing", "awaiting_approval") for app in open_by_id.values()):
        raise AppsError("A chart is being installed or waits for approval; wait for it first.")

    kept: List[ClusterBuildApp] = []
    for raw in raw_items:
        existing = open_by_id.pop(raw.get("id"), None) if isinstance(raw, dict) else None
        item = _normalize_item(raw, existing)
        app = existing
        if app is None:
            # Appended through the parent: SQLAlchemy 2 does not cascade a
            # child into the session from ``ClusterBuildApp(build=...)``.
            app = ClusterBuildApp(created_by=actor or None)
            build.apps.append(app)
        _apply_item(app, item)
        kept.append(app)
    for stale in open_by_id.values():
        build.apps.remove(stale)
    _check_unique(installed + kept)
    for position, app in enumerate(installed + kept):
        app.position = position


def append_apps(build: ClusterBuild, raw_items: Any, *, actor: str = "",
                profile_ids: Optional[List[Optional[int]]] = None) -> List[ClusterBuildApp]:
    """Day two: new charts after the ones already there."""
    if not isinstance(raw_items, list) or not raw_items:
        raise AppsError("Add at least one Helm chart.")
    if len(build.apps) + len(raw_items) > MAX_APPS:
        raise AppsError(f"A cluster gets at most {MAX_APPS} Helm charts from its build.")
    added = []
    for index, raw in enumerate(raw_items):
        item = _normalize_item(raw, None)
        app = ClusterBuildApp(created_by=actor or None, position=len(build.apps))
        build.apps.append(app)
        _apply_item(app, item)
        if profile_ids and index < len(profile_ids):
            app.profile_id = profile_ids[index]
        added.append(app)
    _check_unique(list(build.apps))
    return added


# ---------------------------------------------------------------------------
# Values for an install
# ---------------------------------------------------------------------------

def install_payload(app: ClusterBuildApp, cluster_id: str, *, by_ref: bool) -> Dict[str, Any]:
    """The ``helm_service`` payload for one chart. ``by_ref`` leaves the
    values where they are kept (a request that waits for approval)."""
    payload = {
        "chartSource": "template",
        "chartTemplateId": app.chart_slug,
        "chartVersion": app.chart_version,
        "valuesFile": app.values_file or "",
        "clusterId": cluster_id,
        "namespace": app.namespace,
        "releaseName": app.release_name,
        "environment": "",
    }
    if by_ref:
        payload["valuesRef"] = f"{REF_PREFIX}{app.id}"
    else:
        payload["valuesYaml"] = values_yaml_of(app)
    return payload


def values_for_ref(ref: str, *, slug: str, payload: Dict[str, Any]) -> str:
    """The stored values.yaml a queued install points at.

    Honoured only for exactly the install it was stored for — same chart,
    cluster, namespace and release, still waiting for approval — so a
    reference typed into any other Helm request opens nothing.
    """
    from ..helm_chart_template_service import ChartTemplateError

    refused = ChartTemplateError("valuesRef does not match a chart waiting to be installed.")
    if not ref.startswith(REF_PREFIX):
        raise refused
    try:
        app = db.session.get(ClusterBuildApp, int(ref[len(REF_PREFIX):]))
    except (TypeError, ValueError):
        raise refused from None
    if app is None or app.status != "awaiting_approval" or app.chart_slug != slug:
        raise refused
    build = app.build
    if (
        build is None
        or str(payload.get("clusterId") or "") != str(build.result_cluster_id or "")
        or str(payload.get("namespace") or "").strip() != app.namespace
        or str(payload.get("releaseName") or "").strip().lower() != app.release_name
    ):
        raise refused
    return values_yaml_of(app)


# ---------------------------------------------------------------------------
# Checks before anything is touched
# ---------------------------------------------------------------------------

def _render_locally(app: ClusterBuildApp, values_yaml: str) -> Optional[str]:
    """``helm template`` the chart with its values on the KubeSight server.
    Returns the error, or None when it renders."""
    from ..helm_service import _helm_binary, is_helm_installed

    if not is_helm_installed():
        return ("helm is not installed on the KubeSight server, and KubeSight "
                "installs Helm charts with it.")
    charts = _charts()
    paths: List[str] = []
    try:
        archive = base64.b64decode(charts.chart_archive_base64(app.chart_slug, app.chart_version))
        fd, chart_path = tempfile.mkstemp(suffix=".tgz", prefix="kubesight-app-chart-")
        os.close(fd)
        paths.append(chart_path)
        with open(chart_path, "wb") as handle:
            handle.write(archive)
        fd, values_path = tempfile.mkstemp(suffix=".yaml", prefix="kubesight-app-values-")
        os.close(fd)
        paths.append(values_path)
        with open(values_path, "w", encoding="utf-8") as handle:
            handle.write(values_yaml)
        completed = subprocess.run(
            [_helm_binary(), "template", app.release_name, chart_path,
             "--namespace", app.namespace, "-f", values_path],
            capture_output=True, text=True, check=False, timeout=_RENDER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"helm template took longer than {_RENDER_TIMEOUT_S}s."
    except (OSError, ValueError) as exc:
        return str(exc)
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
    if completed.returncode != 0:
        return scrub((completed.stderr or completed.stdout or "helm template failed").strip())[-1500:]
    return None


def check_app(app: ClusterBuildApp) -> Optional[str]:
    """Why this chart would not install with these values, or None."""
    charts = _charts()
    try:
        values_yaml = charts.build_values_yaml(
            app.chart_slug, {}, app.chart_version, app.values_file or "", values_yaml_of(app)
        )
    except charts.ChartTemplateError as exc:
        return str(exc)
    return _render_locally(app, values_yaml)


def preflight_checks(build: ClusterBuild) -> List[Dict[str, Any]]:
    """One check per chart still to install: its values are complete and it
    renders. Hard failures — the apps phase would fail on them anyway, after
    the whole cluster was built."""
    checks = []
    for app in build.apps:
        if app.status == "installed":
            continue
        label = f"Helm chart {app.release_name} ({app.chart_name or app.chart_slug} {app.chart_version})"
        problem = check_app(app)
        checks.append({
            "id": f"app_{app.id}",
            "label": label,
            "status": "fail" if problem else "pass",
            "detail": problem or f"Renders with its values into namespace {app.namespace}.",
            "hint": "Fix the chart's values in the build's Apps step, then re-run preflight."
            if problem else "",
        })
    return checks


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def cluster_needs_approval(cluster_id: str) -> bool:
    from ..deployment_request_service import cluster_required_approvals

    try:
        return cluster_required_approvals(cluster_id) > 0
    except Exception:  # noqa: BLE001 — fail closed
        return True


def _install_release(payload: Dict[str, Any], approval_context: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """The install itself (a seam for tests)."""
    from ..helm_service import install_or_upgrade_release

    return install_or_upgrade_release(None, payload, "", approval_context=approval_context)


def install_one(build: ClusterBuild, app: ClusterBuildApp, approval_context: str) -> str:
    """Install one chart onto the build's cluster. Returns helm's output;
    raises AppsError with helm's message when it fails."""
    from ..helm_service import record_helm_catalog_entry

    app.status = "installing"
    app.error = None
    db.session.commit()
    payload = install_payload(app, build.result_cluster_id, by_ref=False)
    data, error, status = _install_release(payload, approval_context)
    if error or status >= 300:
        app.status = "failed"
        app.error = scrub(error or f"helm returned status {status}")[:2000]
        db.session.commit()
        raise AppsError(app.error)
    mark_installed(app)
    try:
        record_helm_catalog_entry(None, {**payload, "chartName": app.chart_name})
    except Exception:  # noqa: BLE001 — the release is in; the catalog is secondary
        pass
    return str((data or {}).get("output") or "")


def mark_installed(app: ClusterBuildApp) -> None:
    app.status = "installed"
    app.error = None
    app.installed_at = _utcnow()
    # Nothing reads the values once the chart is in: do not keep the passwords.
    app.values_cipher = None
    db.session.commit()


def advance_queued() -> int:
    """Scheduler tick: day-two charts that waited for the cluster's approval
    take their change bundle's outcome. Returns how many moved."""
    from ...models import ChangeBundle

    moved = 0
    for app in ClusterBuildApp.query.filter_by(status="awaiting_approval").all():
        bundle = db.session.get(ChangeBundle, app.bundle_id) if app.bundle_id else None
        if bundle is None:
            app.status, app.error = "failed", "Its approval request no longer exists."
        elif bundle.status in ("rejected", "expired", "cancelled", "withdrawn"):
            app.status, app.error = "rejected", f"Change bundle #{bundle.id} was {bundle.status}."
        elif bundle.status in ("completed", "failed", "partially_failed"):
            item = next(
                (i for i in bundle.items if i.resource_name == app.release_name
                 and i.namespace == app.namespace), None,
            )
            if item is not None and item.status == "succeeded":
                mark_installed(app)
                moved += 1
                continue
            result = (item.execution_result or {}) if item is not None else {}
            app.status = "failed"
            app.error = scrub(str(result.get("error") or result.get("message")
                                 or f"Change bundle #{bundle.id} {bundle.status}."))[:2000]
        else:
            continue
        db.session.commit()
        moved += 1
    return moved
