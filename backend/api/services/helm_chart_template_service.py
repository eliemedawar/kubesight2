"""Reusable Helm chart catalog and Kubernetes-manifest-to-chart conversion."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import zipfile
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import yaml

from sqlalchemy import func

from ..audit import log_audit
from ..db import db
from ..models import HelmChartTemplate, HelmChartTemplateVersion

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_FILES = 500
MAX_VARIABLES = 250
MAX_GIT_PATHS = 2000
# Archiver leftovers that would otherwise be stored inside the chart.
ARCHIVE_JUNK_FILES = {".DS_Store", "Thumbs.db"}
ARCHIVE_JUNK_DIRS = {"__MACOSX", ".git"}
ARCHIVE_SUFFIXES = (".zip", ".tgz", ".tar.gz")
ARCHIVE_SUFFIX_LABEL = ".zip, .tgz, or .tar.gz"
TEMPLATE_KIND_RE = re.compile(r"(?m)^\s*kind:\s*[\"']?([A-Za-z][A-Za-z0-9.-]*)[\"']?\s*(?:#.*)?$")
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|client[_-]?secret|auth)",
    re.IGNORECASE,
)
# Keys that only *point at* a credential, or opt out of being one, never hold the
# material itself: `imagePullSecrets` is a list of Secret names, `secrets.
# existingSecretName` names a Secret created out of band, and `tmoptions.nonSecret`
# says so outright. Blanking those breaks the chart and demands pointless input.
SENSITIVE_REFERENCE_RE = re.compile(
    r"^(?:"
    r"non[_-]?(?:secret|credential)s?"
    r"|image[_-]?pull[_-]?secrets?"
    r"|(?:existing|external)[_-]?(?:secret|credential|token)[a-z0-9_-]*"
    r"|[a-z0-9_-]*(?:secret|token|credential|password)s?[_-]?(?:name|names|ref|refs)"
    r"|(?:secret|token|credential|auth)s?[_-]?(?:enabled|type|mode|provider|store|class|namespace)"
    r"|auth[_-]?(?:enabled|type|mode|url|endpoint|method|header|headers|proxy)"
    r")$",
    re.IGNORECASE,
)
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9._/@+-]+$")
RUNTIME_METADATA_KEYS = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
}


class ChartTemplateError(ValueError):
    pass


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return (slug or "helm-chart")[:100]


def _unique_slug(value: str) -> str:
    base = _slugify(value)
    candidate = base
    suffix = 2
    while HelmChartTemplate.query.filter_by(slug=candidate).first():
        candidate = f"{base[:110 - len(str(suffix))]}-{suffix}"
        suffix += 1
    return candidate


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _definition_counts(definition: Dict[str, Any]) -> Dict[str, Any]:
    chart = definition.get("chart") or {}
    variables = definition.get("variables") or []
    return {
        "templateCount": len(chart.get("templates") or []),
        "valuesFileCount": len(chart.get("valuesFiles") or []),
        "variableCount": len(variables),
        "requiredVariableCount": sum(1 for item in variables if item.get("required")),
        "resourceCount": int(definition.get("resourceCount") or 0),
    }


def _version_entry(
    *,
    version: str,
    app_version: str,
    description: str,
    source_type: str,
    source_ref: str,
    created_at: Any,
    created_by: Optional[int],
    definition: Dict[str, Any],
    is_current: bool,
) -> Dict[str, Any]:
    """A version list entry. Deliberately free of the heavy per-version chart
    inspection so the catalog listing stays small; fetch the version's detail
    for its files, values and inputs."""
    return {
        "version": version or "0.1.0",
        "appVersion": app_version or "",
        "description": description or "",
        "sourceType": source_type,
        "sourceRef": source_ref or "",
        "createdAt": created_at.isoformat() if created_at else None,
        "createdBy": created_by,
        "isCurrent": bool(is_current),
        **_definition_counts(definition or {}),
    }


def _version_entries(row: HelmChartTemplate) -> List[Dict[str, Any]]:
    """Version list for a chart. Charts imported before versioning existed have
    no version rows yet, so the parent row itself stands in as their only
    version until something writes (see ``_backfill_versions``)."""
    rows = sorted(
        row.versions or [],
        key=lambda item: (item.created_at is None, item.created_at, item.id),
    )
    if not rows:
        return [
            _version_entry(
                version=row.version,
                app_version=row.app_version,
                description=row.description,
                source_type=row.source_type,
                source_ref=row.source_ref,
                created_at=row.created_at,
                created_by=row.created_by,
                definition=row.definition or {},
                is_current=True,
            )
        ]
    return [
        _version_entry(
            version=item.version,
            app_version=item.app_version,
            description=item.description,
            source_type=item.source_type,
            source_ref=item.source_ref,
            created_at=item.created_at,
            created_by=item.created_by,
            definition=item.definition or {},
            is_current=item.version == row.version,
        )
        for item in rows
    ]


def _summary(
    row: HelmChartTemplate,
    *,
    version_count: Optional[int] = None,
    versions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    definition = row.definition or {}
    chart = definition.get("chart") or {}
    return {
        "id": row.slug,
        "name": row.name,
        "description": row.description or "",
        "version": row.version or "0.1.0",
        "appVersion": row.app_version or "",
        "sourceType": row.source_type,
        "sourceRef": row.source_ref or "",
        "chart": chart,
        "versionCount": version_count if version_count is not None else len(versions or [1]),
        "versions": versions or [],
        **_definition_counts(definition),
        "warnings": definition.get("warnings") or [],
        "createdBy": row.created_by,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _detail(row: HelmChartTemplate, version: str = "") -> Dict[str, Any]:
    """Chart detail. ``version`` scopes the inputs and defaults to one revision;
    without it the currently selected version is described."""
    versions = _version_entries(row)
    definition = _definition_for(row, version)
    selected = _clean_text(version, 64) or (row.version or "0.1.0")
    entry = next((item for item in versions if item["version"] == selected), None)
    chart = deepcopy(definition.get("chart") or {})
    environment_values = _environment_values_files(definition)
    for item in chart.get("valuesFiles") or []:
        item["values"] = deepcopy(environment_values.get(item.get("path")) or {})
    return {
        **_summary(row, versions=versions),
        "version": selected,
        "appVersion": (entry or {}).get("appVersion") or row.app_version or "",
        "sourceType": (entry or {}).get("sourceType") or row.source_type,
        "sourceRef": (entry or {}).get("sourceRef") or row.source_ref or "",
        "chart": chart,
        **_definition_counts(definition),
        "warnings": definition.get("warnings") or [],
        "variables": definition.get("variables") or [],
        "defaultValues": definition.get("values") or {},
    }


def list_chart_templates() -> List[Dict[str, Any]]:
    rows = HelmChartTemplate.query.order_by(
        HelmChartTemplate.name.asc(), HelmChartTemplate.id.asc()
    ).all()
    counts = dict(
        db.session.query(
            HelmChartTemplateVersion.template_id, func.count(HelmChartTemplateVersion.id)
        )
        .group_by(HelmChartTemplateVersion.template_id)
        .all()
    )
    return [
        _summary(row, version_count=counts.get(row.id) or 1, versions=_version_entries(row))
        for row in rows
    ]


def get_chart_template(slug: str, version: str = "") -> Optional[Dict[str, Any]]:
    row = HelmChartTemplate.query.filter_by(slug=slug).first()
    return _detail(row, version) if row else None


def _get_row(slug: str) -> HelmChartTemplate:
    row = HelmChartTemplate.query.filter_by(slug=slug).first()
    if not row:
        raise ChartTemplateError("Helm chart template not found.")
    return row


def _version_row(row: HelmChartTemplate, version: str) -> Optional[HelmChartTemplateVersion]:
    wanted = _clean_text(version, 64)
    if not wanted:
        return None
    return HelmChartTemplateVersion.query.filter_by(
        template_id=row.id, version=wanted
    ).first()


def _definition_for(row: HelmChartTemplate, version: str = "") -> Dict[str, Any]:
    """The stored definition of one version, defaulting to the current one."""
    wanted = _clean_text(version, 64)
    if not wanted or wanted == (row.version or ""):
        return row.definition or {}
    found = _version_row(row, wanted)
    if not found:
        raise ChartTemplateError(f"Version {wanted} was not found for {row.name}.")
    return found.definition or {}


def _safe_chart_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ChartTemplateError("Chart contains an unsafe file path.")
    if not all(SAFE_FILE_RE.match(part) for part in parts):
        raise ChartTemplateError(f"Chart file path is not supported: {normalized}")
    return "/".join(parts)


def _is_sensitive_key_path(parts: Sequence[Any]) -> bool:
    """True when a values path names credential material rather than a pointer.

    The decision is made on the leaf key: a reference or opt-out name wins even
    inside a section called ``secrets``, while a genuinely sensitive leaf stays
    sensitive wherever it sits.
    """
    names = [str(part) for part in parts if not isinstance(part, int)]
    if not names:
        return False
    if SENSITIVE_REFERENCE_RE.match(names[-1].strip()):
        return False
    return any(SENSITIVE_KEY_RE.search(name) for name in names)


def _is_environment_values_file(path: str) -> bool:
    """True for optional per-environment values files such as ``values-prod.yaml``."""
    if path == "values.yaml" or path.startswith("templates/"):
        return False
    name = Path(path).name.lower()
    return name.startswith("values") and name.endswith((".yaml", ".yml"))


def _values_file_environment(path: str) -> str:
    stem = Path(path).stem
    label = re.sub(r"^values", "", stem, flags=re.IGNORECASE).strip(" -_.")
    return label or "values"


def _chart_inspection(files: Dict[str, bytes]) -> Dict[str, Any]:
    """Describe a stored chart: Chart.yaml metadata, templates and values files."""
    metadata: Dict[str, Any] = {}
    if files.get("Chart.yaml"):
        try:
            loaded = yaml.safe_load(files["Chart.yaml"].decode("utf-8"))
            metadata = loaded if isinstance(loaded, dict) else {}
        except (UnicodeDecodeError, yaml.YAMLError):
            metadata = {}

    templates: List[Dict[str, Any]] = []
    values_files: List[Dict[str, Any]] = []
    for path in sorted(files):
        if path.startswith("templates/"):
            kinds: List[str] = []
            if path.lower().endswith((".yaml", ".yml")):
                try:
                    kinds = sorted(set(TEMPLATE_KIND_RE.findall(files[path].decode("utf-8"))))
                except UnicodeDecodeError:
                    kinds = []
            templates.append({"path": path, "kinds": kinds})
        elif _is_environment_values_file(path):
            keys: List[str] = []
            try:
                loaded = yaml.safe_load(files[path].decode("utf-8"))
                if isinstance(loaded, dict):
                    keys = sorted(str(key) for key in loaded)
            except (UnicodeDecodeError, yaml.YAMLError):
                keys = []
            # The parsed contents are attached per request in ``_detail`` instead of
            # being stored here, so the catalog listing stays small.
            values_files.append(
                {
                    "path": path,
                    "environment": _values_file_environment(path),
                    "keyCount": len(keys),
                    "topLevelKeys": keys[:25],
                }
            )

    return {
        "apiVersion": str(metadata.get("apiVersion") or ""),
        "type": str(metadata.get("type") or ""),
        "hasChartYaml": "Chart.yaml" in files,
        "hasValuesYaml": "values.yaml" in files,
        "templates": templates,
        "valuesFiles": values_files,
        "fileCount": len(files),
    }


def _encode_files(files: Dict[str, bytes]) -> Dict[str, str]:
    total = 0
    if len(files) > MAX_IMPORT_FILES:
        raise ChartTemplateError(f"Charts may contain at most {MAX_IMPORT_FILES} files.")
    encoded: Dict[str, str] = {}
    for raw_path, content in files.items():
        path = _safe_chart_path(raw_path)
        total += len(content)
        if total > MAX_IMPORT_BYTES:
            raise ChartTemplateError("Chart content exceeds the 10 MiB import limit.")
        encoded[path] = base64.b64encode(content).decode("ascii")
    return encoded


def _definition_blob(
    files: Dict[str, bytes],
    values: Dict[str, Any],
    variables: List[Dict[str, Any]],
    warnings: List[str],
    resource_count: int,
) -> Dict[str, Any]:
    return {
        "files": _encode_files(files),
        "values": values,
        "variables": variables[:MAX_VARIABLES],
        "warnings": warnings,
        "resourceCount": resource_count,
        "chart": _chart_inspection(files),
    }


def _decode_files(definition: Dict[str, Any]) -> Dict[str, bytes]:
    decoded: Dict[str, bytes] = {}
    for raw_path, content in (definition.get("files") or {}).items():
        path = _safe_chart_path(raw_path)
        try:
            decoded[path] = base64.b64decode(content, validate=True)
        except (ValueError, TypeError) as exc:
            raise ChartTemplateError(f"Stored chart file is invalid: {path}") from exc
    return decoded


def _environment_values_files(definition: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return the scrubbed values mappings users may select at deploy time."""
    files = _decode_files(definition)
    values_files: Dict[str, Dict[str, Any]] = {}
    for path, content in files.items():
        if not _is_environment_values_file(path):
            continue
        try:
            loaded = yaml.safe_load(content.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            loaded = None
        if isinstance(loaded, dict):
            values_files[path] = loaded
    return values_files


def _merge_values(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Apply Helm-style map merging: maps merge, other values replace."""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_values(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _persist(
    *,
    name: str,
    description: str,
    version: str,
    app_version: str,
    source_type: str,
    source_ref: str,
    files: Dict[str, bytes],
    values: Dict[str, Any],
    variables: List[Dict[str, Any]],
    warnings: List[str],
    resource_count: int,
    actor_user_id: Optional[int],
) -> Dict[str, Any]:
    clean_name = _clean_text(name, 120)
    if not clean_name:
        raise ChartTemplateError("Chart name is required.")
    clean_version = _clean_text(version, 64) or "0.1.0"
    definition = _definition_blob(files, values, variables, warnings, resource_count)
    row = HelmChartTemplate(
        slug=_unique_slug(clean_name),
        name=clean_name,
        description=_clean_text(description, 500) or None,
        version=clean_version,
        app_version=_clean_text(app_version, 64) or None,
        source_type=source_type,
        source_ref=_clean_text(source_ref, 1000) or None,
        definition=definition,
        created_by=actor_user_id,
    )
    row.versions.append(
        HelmChartTemplateVersion(
            version=clean_version,
            app_version=_clean_text(app_version, 64) or None,
            description=_clean_text(description, 500) or None,
            source_type=source_type,
            source_ref=_clean_text(source_ref, 1000) or None,
            definition=definition,
            created_by=actor_user_id,
        )
    )
    db.session.add(row)
    db.session.commit()
    log_audit(
        "helm_chart_template_created",
        actor_user_id=actor_user_id,
        target_type="helm_chart_template",
        target_id=row.slug,
        details={
            "name": row.name,
            "sourceType": source_type,
            "version": clean_version,
            "variableCount": len(variables),
            "resourceCount": resource_count,
        },
    )
    return _detail(row)


def _backfill_versions(row: HelmChartTemplate) -> None:
    """Give a pre-versioning chart its first version row from the parent state."""
    if row.versions:
        return
    row.versions.append(
        HelmChartTemplateVersion(
            version=row.version or "0.1.0",
            app_version=row.app_version,
            description=row.description,
            source_type=row.source_type,
            source_ref=row.source_ref,
            definition=row.definition or {},
            created_by=row.created_by,
            created_at=row.created_at,
        )
    )
    db.session.flush()


def _persist_version(
    row: HelmChartTemplate,
    prepared: Dict[str, Any],
    *,
    make_current: bool,
    actor_user_id: Optional[int],
) -> Dict[str, Any]:
    version = _clean_text(prepared.get("version"), 64) or "0.1.0"
    _backfill_versions(row)
    if any(item.version == version for item in row.versions):
        raise ChartTemplateError(
            f"Version {version} already exists for {row.name}. "
            "Bump the version in Chart.yaml or delete the stored version first."
        )
    app_version = _clean_text(prepared.get("app_version"), 64) or None
    description = _clean_text(prepared.get("description"), 500) or None
    source_ref = _clean_text(prepared.get("source_ref"), 1000) or None
    definition = _definition_blob(
        prepared["files"],
        prepared["values"],
        prepared["variables"],
        prepared["warnings"],
        prepared["resource_count"],
    )
    row.versions.append(
        HelmChartTemplateVersion(
            version=version,
            app_version=app_version,
            description=description,
            source_type=prepared["source_type"],
            source_ref=source_ref,
            definition=definition,
            created_by=actor_user_id,
        )
    )
    if make_current:
        row.version = version
        row.app_version = app_version
        row.source_type = prepared["source_type"]
        row.source_ref = source_ref
        row.definition = definition
        # The catalog description belongs to the chart, not the upload: only fill
        # it in when it is still empty so a curated description is never lost.
        if description and not row.description:
            row.description = description
    db.session.commit()
    log_audit(
        "helm_chart_template_version_added",
        actor_user_id=actor_user_id,
        target_type="helm_chart_template",
        target_id=row.slug,
        details={
            "name": row.name,
            "version": version,
            "sourceType": prepared["source_type"],
            "isCurrent": bool(make_current),
        },
    )
    return _detail(row, version)


def add_chart_version(
    slug: str, payload: Dict[str, Any], actor_user_id: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Add another archive as a new version of an existing chart."""
    row = HelmChartTemplate.query.filter_by(slug=slug).first()
    if not row:
        return None, "Helm chart template not found.", 404
    try:
        prepared = _prepare_archive_import(payload)
        make_current = payload.get("makeCurrent")
        data = _persist_version(
            row,
            prepared,
            make_current=True if make_current is None else bool(make_current),
            actor_user_id=actor_user_id,
        )
        return data, None, 201
    except ChartTemplateError as exc:
        db.session.rollback()
        return None, str(exc), 400
    except (OSError, UnicodeError) as exc:
        db.session.rollback()
        return None, f"Unable to read the archive: {exc}", 400


def set_current_chart_version(
    slug: str, version: str, actor_user_id: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    row = HelmChartTemplate.query.filter_by(slug=slug).first()
    if not row:
        return None, "Helm chart template not found.", 404
    _backfill_versions(row)
    wanted = _clean_text(version, 64)
    found = next((item for item in row.versions if item.version == wanted), None)
    if not found:
        db.session.rollback()
        return None, f"Version {wanted} was not found for {row.name}.", 404
    row.version = found.version
    row.app_version = found.app_version
    row.source_type = found.source_type
    row.source_ref = found.source_ref
    row.definition = found.definition or {}
    db.session.commit()
    log_audit(
        "helm_chart_template_version_selected",
        actor_user_id=actor_user_id,
        target_type="helm_chart_template",
        target_id=row.slug,
        details={"name": row.name, "version": found.version},
    )
    return _detail(row), None, 200


def delete_chart_version(
    slug: str, version: str, actor_user_id: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    row = HelmChartTemplate.query.filter_by(slug=slug).first()
    if not row:
        return None, "Helm chart template not found.", 404
    _backfill_versions(row)
    wanted = _clean_text(version, 64)
    found = next((item for item in row.versions if item.version == wanted), None)
    if not found:
        db.session.rollback()
        return None, f"Version {wanted} was not found for {row.name}.", 404
    if len(row.versions) == 1:
        db.session.rollback()
        return (
            None,
            "This is the only version of the chart. Delete the chart itself instead.",
            400,
        )
    was_current = found.version == row.version
    row.versions.remove(found)
    db.session.flush()
    if was_current:
        newest = sorted(
            row.versions, key=lambda item: (item.created_at is None, item.created_at, item.id)
        )[-1]
        row.version = newest.version
        row.app_version = newest.app_version
        row.source_type = newest.source_type
        row.source_ref = newest.source_ref
        row.definition = newest.definition or {}
    db.session.commit()
    log_audit(
        "helm_chart_template_version_deleted",
        actor_user_id=actor_user_id,
        target_type="helm_chart_template",
        target_id=row.slug,
        details={"name": row.name, "version": wanted, "wasCurrent": was_current},
    )
    return _detail(row), None, 200


def delete_chart_template(
    slug: str, actor_user_id: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    row = HelmChartTemplate.query.filter_by(slug=slug).first()
    if not row:
        return None, "Helm chart template not found.", 404
    name = row.name
    version_count = len(row.versions)
    db.session.delete(row)
    db.session.commit()
    log_audit(
        "helm_chart_template_deleted",
        actor_user_id=actor_user_id,
        target_type="helm_chart_template",
        target_id=slug,
        details={"name": name, "versionCount": version_count},
    )
    return {"id": slug, "deleted": True}, None, 200


def _clean_resource(doc: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = deepcopy(doc)
    cleaned.pop("status", None)
    metadata = cleaned.get("metadata")
    if isinstance(metadata, dict):
        for key in RUNTIME_METADATA_KEYS:
            metadata.pop(key, None)
        metadata.pop("namespace", None)
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
            if not annotations:
                metadata.pop("annotations", None)
    return cleaned


def _resource_identity(doc: Dict[str, Any]) -> Tuple[str, str, str]:
    metadata = doc.get("metadata") or {}
    return (
        str(doc.get("apiVersion") or ""),
        str(doc.get("kind") or ""),
        str(metadata.get("name") or ""),
    )


def _safe_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    if not key or key[0].isdigit():
        key = f"value_{key}"
    return key[:80]


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _set_nested(target: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[path[-1]] = value


def _get_nested(target: Dict[str, Any], path: Sequence[str], default: Any = None) -> Any:
    cursor: Any = target
    for part in path:
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


class _VariableBuilder:
    def __init__(self) -> None:
        self.values: Dict[str, Any] = {"variables": {}}
        self.variables: List[Dict[str, Any]] = []
        self._keys: set[str] = set()
        self._sentinel_index = 0

    def add(
        self,
        parent: Dict[str, Any] | List[Any],
        field: str | int,
        *,
        key: str,
        label: str,
        default: Any,
        category: str,
        required: bool = False,
        sensitive: bool = False,
        transform: str = "",
    ) -> None:
        if len(self.variables) >= MAX_VARIABLES:
            if sensitive:
                parent[field] = ""
                raise ChartTemplateError(
                    f"These resources hold more than {MAX_VARIABLES} secret keys, which is "
                    "past the chart value limit — the remaining ones could not be exposed as "
                    "fields, and their values are never stored. Import fewer resources at a time."
                )
            return
        base = _safe_key(key)
        unique = base
        suffix = 2
        while unique in self._keys:
            unique = f"{base[:72]}_{suffix}"
            suffix += 1
        self._keys.add(unique)
        self._sentinel_index += 1
        sentinel = f"__KUBESIGHT_HELM_VARIABLE_{self._sentinel_index}__"
        parent[field] = sentinel
        safe_default = "" if sensitive else default
        self.values["variables"][unique] = safe_default
        self.variables.append(
            {
                "key": unique,
                "path": f"variables.{unique}",
                "label": label,
                "type": _value_type(default),
                "default": safe_default,
                "required": bool(required or sensitive),
                "sensitive": bool(sensitive),
                "category": category,
                "_sentinel": sentinel,
                "_transform": transform,
            }
        )

    def add_secret_object(self, doc: Dict[str, Any], identity: str) -> set[Tuple[Any, ...]]:
        """Expose one safe object field for every key carried by a large Secret.

        The object stored in values.yaml contains only the Secret's field/key
        names and blank strings.  Each template leaf still has its own Helm
        ``required`` check, so grouping the form input does not weaken deploy
        validation or retain any live credential material.
        """
        blank_sections: Dict[str, Dict[str, str]] = {}
        for section in ("data", "stringData"):
            mapping = doc.get(section)
            if isinstance(mapping, dict) and mapping:
                blank_sections[section] = {str(key): "" for key in mapping}
        if not blank_sections:
            return set()
        if len(self.variables) >= MAX_VARIABLES:
            raise ChartTemplateError(
                f"Chart contains more than {MAX_VARIABLES} Secrets and cannot expose "
                "all of them safely. Import fewer resources at a time."
            )

        base = _safe_key(f"{identity}_values")
        unique = base
        suffix = 2
        while unique in self._keys:
            unique = f"{base[:72]}_{suffix}"
            suffix += 1
        self._keys.add(unique)

        replacements: List[Dict[str, Any]] = []
        added: set[Tuple[Any, ...]] = set()
        for section, keys in blank_sections.items():
            mapping = doc[section]
            for secret_key in keys:
                self._sentinel_index += 1
                sentinel = f"__KUBESIGHT_HELM_VARIABLE_{self._sentinel_index}__"
                mapping[secret_key] = sentinel
                replacements.append(
                    {
                        "sentinel": sentinel,
                        "keys": [section, secret_key],
                        "transform": "b64enc" if section == "data" else "",
                    }
                )
                added.add((section, secret_key))

        safe_default = deepcopy(blank_sections)
        self.values["variables"][unique] = safe_default
        self.variables.append(
            {
                "key": unique,
                "path": f"variables.{unique}",
                "label": f"{identity} values",
                "type": "object",
                "default": deepcopy(safe_default),
                "required": True,
                "sensitive": True,
                "category": "Secrets",
                "description": (
                    "Enter a YAML or JSON mapping with a non-empty value for every listed "
                    "Secret key."
                ),
                "_secret_replacements": replacements,
            }
        )
        return added


def _pod_spec(doc: Dict[str, Any]) -> Dict[str, Any]:
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        return (
            (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
            or {}
        )
    return ((spec.get("template") or {}).get("spec") or {})


def _walk_scalar_paths(value: Any, prefix: Tuple[Any, ...] = ()) -> Iterable[Tuple[Tuple[Any, ...], Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_scalar_paths(child, prefix + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_scalar_paths(child, prefix + (index,))
    elif value is not None:
        yield prefix, value


def _path_value(value: Any, path: Sequence[Any]) -> Any:
    cursor = value
    for part in path:
        if isinstance(part, int):
            if not isinstance(cursor, list) or part >= len(cursor):
                return object()
        elif not isinstance(cursor, dict) or part not in cursor:
            return object()
        cursor = cursor[part]
    return cursor


def _path_parent(value: Any, path: Sequence[Any]) -> Tuple[Any, Any]:
    cursor = value
    for part in path[:-1]:
        cursor = cursor[part]
    return cursor, path[-1]


def _known_variables(
    doc: Dict[str, Any],
    identity: str,
    builder: _VariableBuilder,
    *,
    compact_secrets: bool = False,
) -> set[Tuple[Any, ...]]:
    added: set[Tuple[Any, ...]] = set()
    kind = str(doc.get("kind") or "")
    spec = doc.get("spec") or {}
    if kind in {"Deployment", "StatefulSet"} and "replicas" in spec:
        builder.add(
            spec,
            "replicas",
            key=f"{identity}_replicas",
            label=f"{identity} replicas",
            default=spec["replicas"],
            category="Scaling",
        )
        added.add(("spec", "replicas"))

    pod_spec = _pod_spec(doc)
    for index, container in enumerate(pod_spec.get("containers") or []):
        if not isinstance(container, dict):
            continue
        container_name = str(container.get("name") or f"container-{index + 1}")
        base_path: Tuple[Any, ...]
        if kind == "CronJob":
            base_path = (
                "spec", "jobTemplate", "spec", "template", "spec", "containers", index
            )
        else:
            base_path = ("spec", "template", "spec", "containers", index)
        if container.get("image"):
            builder.add(
                container,
                "image",
                key=f"{identity}_{container_name}_image",
                label=f"{container_name} image",
                default=container["image"],
                category="Images",
            )
            added.add(base_path + ("image",))
        for env_index, env in enumerate(container.get("env") or []):
            if not isinstance(env, dict) or "value" not in env:
                continue
            env_name = str(env.get("name") or f"ENV_{env_index + 1}")
            sensitive = _is_sensitive_key_path((env_name,))
            builder.add(
                env,
                "value",
                key=f"{identity}_{container_name}_env_{env_name}",
                label=env_name,
                default=env.get("value") or "",
                category="Environment",
                required=sensitive,
                sensitive=sensitive,
            )
            added.add(base_path + ("env", env_index, "value"))
        resources = container.get("resources") or {}
        for section in ("requests", "limits"):
            values = resources.get(section) or {}
            for resource_name in ("cpu", "memory"):
                if resource_name not in values:
                    continue
                builder.add(
                    values,
                    resource_name,
                    key=f"{identity}_{container_name}_{section}_{resource_name}",
                    label=f"{container_name} {resource_name} {section}",
                    default=values[resource_name],
                    category="Resources",
                )
                added.add(base_path + ("resources", section, resource_name))

    if kind == "Service":
        for index, port in enumerate(spec.get("ports") or []):
            if not isinstance(port, dict):
                continue
            for field in ("port", "targetPort"):
                if field in port and isinstance(port[field], (str, int)):
                    builder.add(
                        port,
                        field,
                        key=f"{identity}_{field}_{index + 1}",
                        label=f"Service {field} {index + 1}",
                        default=port[field],
                        category="Networking",
                    )
                    added.add(("spec", "ports", index, field))

    if kind == "Ingress":
        for index, rule in enumerate(spec.get("rules") or []):
            if isinstance(rule, dict) and rule.get("host"):
                builder.add(
                    rule,
                    "host",
                    key=f"{identity}_host_{index + 1}",
                    label=f"Ingress host {index + 1}",
                    default=rule["host"],
                    category="Networking",
                )
                added.add(("spec", "rules", index, "host"))

    if kind == "PersistentVolumeClaim":
        requests = ((spec.get("resources") or {}).get("requests") or {})
        if requests.get("storage"):
            builder.add(
                requests,
                "storage",
                key=f"{identity}_storage",
                label=f"{identity} storage size",
                default=requests["storage"],
                category="Storage",
            )
            added.add(("spec", "resources", "requests", "storage"))

    if kind == "ConfigMap":
        for field in ("data", "binaryData"):
            data = doc.get(field) or {}
            for key in list(data):
                builder.add(
                    data,
                    key,
                    key=f"{identity}_{field}_{key}",
                    label=f"{identity} {key}",
                    default=data[key],
                    category="Configuration",
                )
                added.add((field, key))

    if kind == "Secret":
        if compact_secrets:
            return added | builder.add_secret_object(doc, identity)
        for field in ("data", "stringData"):
            data = doc.get(field) or {}
            for key in list(data):
                builder.add(
                    data,
                    key,
                    key=f"{identity}_{key}",
                    label=f"{identity} {key}",
                    default="",
                    category="Secrets",
                    required=True,
                    sensitive=True,
                    transform="b64enc" if field == "data" else "",
                )
                added.add((field, key))
    return added


def _different_scalar_paths(base: Any, variants: Sequence[Any]) -> List[Tuple[Any, ...]]:
    paths: List[Tuple[Any, ...]] = []
    for path, value in _walk_scalar_paths(base):
        if any(_path_value(variant, path) != value for variant in variants):
            paths.append(path)
    return paths


def _render_manifest_template(doc: Dict[str, Any], variables: Sequence[Dict[str, Any]]) -> str:
    dumped = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    for item in variables:
        secret_replacements = item.get("_secret_replacements") or []
        if secret_replacements:
            for replacement in secret_replacements:
                sentinel = replacement.get("sentinel")
                if not sentinel or sentinel not in dumped:
                    continue
                indexes = " ".join(
                    json.dumps(str(key)) for key in replacement.get("keys") or []
                )
                value_ref = f'(index .Values.{item["path"]} {indexes})'
                key_label = "/".join(str(key) for key in replacement.get("keys") or [])
                label = f'{item["label"]} {key_label}'
                expression = f'required "{label} is required" {value_ref}'
                if replacement.get("transform") == "b64enc":
                    expression = f"{expression} | b64enc"
                dumped = dumped.replace(sentinel, "{{ " + expression + " | quote }}")
            continue
        sentinel = item.get("_sentinel")
        if not sentinel or sentinel not in dumped:
            continue
        value_ref = f".Values.{item['path']}"
        if item.get("required"):
            expression = f'required "{item["label"]} is required" {value_ref}'
        else:
            expression = value_ref
        if item.get("_transform") == "b64enc":
            expression = f"{expression} | b64enc"
        if item.get("type") == "string":
            expression = f"{expression} | quote"
        dumped = dumped.replace(sentinel, "{{ " + expression + " }}")
    return dumped


def _public_variables(variables: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in variables
    ]


def _manifest_files(payload: Dict[str, Any]) -> List[Tuple[str, str]]:
    raw_files = payload.get("files")
    if isinstance(raw_files, list):
        result: List[Tuple[str, str]] = []
        total = 0
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict):
                continue
            name = _clean_text(item.get("name") or f"manifest-{index + 1}.yaml", 255)
            content = str(item.get("content") or "")
            total += len(content.encode("utf-8"))
            if total > MAX_IMPORT_BYTES:
                raise ChartTemplateError("YAML content exceeds the 10 MiB import limit.")
            if content.strip():
                result.append((name, content))
        return result
    yaml_text = str(payload.get("yaml") or payload.get("yamlContent") or "")
    return [("manifests.yaml", yaml_text)] if yaml_text.strip() else []


def _convert_manifest_files(
    source_files: Sequence[Tuple[str, str]],
    *,
    requested_name: str = "",
) -> Tuple[str, Dict[str, bytes], Dict[str, Any], List[Dict[str, Any]], List[str], int]:
    if not source_files:
        raise ChartTemplateError("At least one Kubernetes YAML file is required.")

    grouped: Dict[Tuple[str, str, str], List[Tuple[str, Dict[str, Any]]]] = {}
    warnings: List[str] = []
    for filename, content in source_files:
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError as exc:
            raise ChartTemplateError(f"Invalid YAML in {filename}: {exc}") from exc
        for doc in docs:
            if not isinstance(doc, dict) or not doc.get("kind"):
                continue
            cleaned = _clean_resource(doc)
            identity = _resource_identity(cleaned)
            if not identity[1] or not identity[2]:
                warnings.append(f"Skipped an unnamed {identity[1] or 'resource'} in {filename}.")
                continue
            grouped.setdefault(identity, []).append((filename, cleaned))

    if not grouped:
        raise ChartTemplateError("No named Kubernetes resources were found.")

    inferred_name = requested_name
    if not inferred_name:
        workload = next((key for key in grouped if key[1] in WORKLOAD_KINDS), None)
        inferred_name = (workload or next(iter(grouped)))[2]
    name = _clean_text(inferred_name, 120)
    slug = _slugify(name)
    builder = _VariableBuilder()
    template_files: Dict[str, bytes] = {}
    secret_key_count = sum(
        len(mapping)
        for (_, kind, _), entries in grouped.items()
        if kind == "Secret"
        for section in ("data", "stringData")
        for mapping in [entries[0][1].get(section)]
        if isinstance(mapping, dict)
    )
    compact_secrets = secret_key_count > MAX_VARIABLES
    if compact_secrets:
        warnings.append(
            f"This chart contains {secret_key_count} Secret keys. They were compacted into "
            "one required YAML/JSON object per Secret so the chart remains deployable without "
            "storing any live Secret values."
        )

    # Secrets get first claim on the value budget. A live Secret value can only
    # be stored safely by becoming a required field, so a chart that runs out of
    # budget before reaching one has to abort (see _VariableBuilder.add) — while
    # every other resource simply stops being parameterised and keeps its
    # literal value. Sorting is stable, so nothing else changes order.
    ordered = sorted(grouped.items(), key=lambda item: 0 if item[0][1] == "Secret" else 1)

    for index, (resource_key, entries) in enumerate(ordered, start=1):
        _, kind, resource_name = resource_key
        base = deepcopy(entries[0][1])
        identity = _safe_key(f"{kind}_{resource_name}")
        known_paths = _known_variables(
            base, identity, builder, compact_secrets=compact_secrets
        )
        if len(entries) > 1:
            variant_names = ", ".join(entry[0] for entry in entries[1:])
            warnings.append(
                f"{kind}/{resource_name} appeared in multiple files. The first version is the "
                f"default and differing scalar fields from {variant_names} became values."
            )
            for path in _different_scalar_paths(entries[0][1], [entry[1] for entry in entries[1:]]):
                if path in known_paths or not path:
                    continue
                try:
                    parent, field = _path_parent(base, path)
                    default = entries[0][1]
                    for part in path:
                        default = default[part]
                except (KeyError, IndexError, TypeError):
                    continue
                if not isinstance(default, (str, int, float, bool)):
                    continue
                path_label = " ".join(str(part) for part in path if not isinstance(part, int))
                sensitive = _is_sensitive_key_path(path)
                builder.add(
                    parent,
                    field,
                    key=f"{identity}_{'_'.join(str(p) for p in path)}",
                    label=f"{kind}/{resource_name} {path_label}",
                    default=default,
                    category="Variant values",
                    required=sensitive,
                    sensitive=sensitive,
                )

        relative = f"templates/{index:03d}-{_slugify(kind)}-{_slugify(resource_name)}.yaml"
        template_files[relative] = _render_manifest_template(base, builder.variables).encode("utf-8")

    public_variables = _public_variables(builder.variables)
    if len(builder.variables) >= MAX_VARIABLES:
        warnings.append(
            f"This chart hit the {MAX_VARIABLES}-value limit. Every secret was still exposed "
            "as a required field; the remaining fields kept the literal values they had. "
            "Import fewer resources at a time for a fully configurable chart."
        )
    chart_yaml = {
        "apiVersion": "v2",
        "name": slug,
        "description": f"Imported Kubernetes resources for {name}",
        "type": "application",
        "version": "0.1.0",
        "appVersion": "1.0.0",
    }
    template_files["Chart.yaml"] = yaml.safe_dump(chart_yaml, sort_keys=False).encode("utf-8")
    template_files["values.yaml"] = yaml.safe_dump(
        builder.values, sort_keys=False, default_flow_style=False
    ).encode("utf-8")
    return name, template_files, builder.values, public_variables, warnings, len(grouped)


def import_yaml_chart(
    payload: Dict[str, Any], actor_user_id: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    try:
        name, files, values, variables, warnings, count = _convert_manifest_files(
            _manifest_files(payload), requested_name=_clean_text(payload.get("name"), 120)
        )
        data = _persist(
            name=name,
            description=_clean_text(payload.get("description"), 500)
            or "Converted from Kubernetes YAML",
            version="0.1.0",
            app_version="1.0.0",
            source_type="yaml",
            source_ref=", ".join(name for name, _ in _manifest_files(payload))[:1000],
            files=files,
            values=values,
            variables=variables,
            warnings=warnings,
            resource_count=count,
            actor_user_id=actor_user_id,
        )
        return data, None, 201
    except ChartTemplateError as exc:
        return None, str(exc), 400


def _flatten_values(
    value: Any, prefix: Tuple[str, ...] = ()
) -> Iterable[Tuple[Tuple[str, ...], Any]]:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _flatten_values(child, prefix + (str(key),))
    else:
        # Lists are kept as one YAML-friendly value to avoid asking for dozens of
        # indexes, and an empty mapping is a leaf as well so declared-but-empty
        # keys (`podAnnotations: {}`, `tmoptions.envOverrides: {}`) stay editable
        # instead of disappearing from the form. The deploy form offers a YAML
        # textarea for both.
        yield prefix, value


def _schema_for_path(schema: Dict[str, Any], path: Sequence[str]) -> Dict[str, Any]:
    cursor = schema if isinstance(schema, dict) else {}
    for part in path:
        cursor = (cursor.get("properties") or {}).get(part) or {}
    return cursor if isinstance(cursor, dict) else {}


def _path_required(schema: Dict[str, Any], path: Sequence[str]) -> bool:
    cursor = schema if isinstance(schema, dict) else {}
    for part in path:
        if part in (cursor.get("required") or []):
            return True
        cursor = (cursor.get("properties") or {}).get(part) or {}
    return False


def _is_sensitive_value(schema: Dict[str, Any], path: Sequence[str]) -> bool:
    node = _schema_for_path(schema, path)
    return bool(
        _is_sensitive_key_path(path)
        or str(node.get("format") or "").lower() in {"password", "secret"}
        or node.get("writeOnly")
        or node.get("x-sensitive")
    )


def _scrub_chart_values(
    values: Dict[str, Any], schema: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    cleaned = deepcopy(values)
    variables: List[Dict[str, Any]] = []
    warnings: List[str] = []
    flattened = list(_flatten_values(values))
    # Sensitive leaves are processed first so a very large values.yaml can
    # never crowd credentials out of the safe, required-input surface.
    flattened.sort(
        key=lambda item: not _is_sensitive_value(schema, item[0])
    )
    for path, default in flattened:
        if not path:
            continue
        sensitive = _is_sensitive_value(schema, path)
        if len(variables) >= MAX_VARIABLES:
            if sensitive:
                raise ChartTemplateError(
                    f"Chart contains more than {MAX_VARIABLES} values and cannot "
                    "expose all sensitive values safely."
                )
            continue
        schema_node = _schema_for_path(schema, path)
        required = sensitive or _path_required(schema, path)
        safe_default = "" if sensitive else default
        if sensitive:
            _set_nested(cleaned, path, "")
            warnings.append(f"Stored default for sensitive value '{'.'.join(path)}' was removed.")
        value_type = schema_node.get("type") or _value_type(default)
        variables.append(
            {
                "key": "_".join(_safe_key(part) for part in path),
                "path": ".".join(path),
                "label": schema_node.get("title") or " ".join(path),
                "description": schema_node.get("description") or "",
                "type": value_type,
                "default": safe_default,
                "required": bool(required),
                "sensitive": bool(sensitive),
                "category": path[0].replace("_", " ").title(),
            }
        )
    if len(flattened) > MAX_VARIABLES:
        warnings.append(f"Only the first {MAX_VARIABLES} chart values are editable in the form.")
    return cleaned, variables, warnings


def _read_chart_directory(chart_root: Path) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    total = 0
    for path in sorted(chart_root.rglob("*")):
        if not path.is_file() or path.is_symlink() or ".git" in path.parts:
            continue
        relative = path.relative_to(chart_root).as_posix()
        content = path.read_bytes()
        total += len(content)
        if len(files) >= MAX_IMPORT_FILES:
            raise ChartTemplateError(f"Charts may contain at most {MAX_IMPORT_FILES} files.")
        if total > MAX_IMPORT_BYTES:
            raise ChartTemplateError("Chart content exceeds the 10 MiB import limit.")
        files[relative] = content
    return files


def _scrub_schema_secrets(schema: Any, prefix: Tuple[str, ...] = ()) -> None:
    if not isinstance(schema, dict):
        return
    if (
        _is_sensitive_key_path(prefix)
        or str(schema.get("format") or "").lower() in {"password", "secret"}
        or schema.get("writeOnly")
        or schema.get("x-sensitive")
    ):
        for key in ("default", "examples", "enum", "const"):
            schema.pop(key, None)
    for key, child in (schema.get("properties") or {}).items():
        _scrub_schema_secrets(child, prefix + (str(key),))
    if isinstance(schema.get("items"), dict):
        _scrub_schema_secrets(schema["items"], prefix + ("item",))


def _scrub_static_secret_templates(
    files: Dict[str, bytes],
    values: Dict[str, Any],
    variables: List[Dict[str, Any]],
) -> List[str]:
    """Remove literal values from static Secret manifests in an imported chart."""
    warnings: List[str] = []
    counter = 0
    kind_re = re.compile(r"(?m)^\s*kind:\s*Secret\s*(?:#.*)?$")
    section_re = re.compile(r"^(\s*)(data|stringData):\s*(?:#.*)?$")
    entry_re = re.compile(r"^(\s+)([^:#][^:]*):\s*(.*?)\s*$")

    for path, raw in list(files.items()):
        if not path.startswith("templates/") or not path.lower().endswith((".yaml", ".yml")):
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        documents = re.split(r"(?m)(^---\s*$)", content)
        changed = False
        for doc_index in range(0, len(documents), 2):
            document = documents[doc_index]
            if not kind_re.search(document):
                continue
            lines = document.splitlines()
            section = ""
            section_indent = -1
            for line_index, line in enumerate(lines):
                section_match = section_re.match(line)
                if section_match:
                    section_indent = len(section_match.group(1))
                    section = section_match.group(2)
                    continue
                if not section:
                    continue
                entry_match = entry_re.match(line)
                if not entry_match or len(entry_match.group(1)) <= section_indent:
                    if line.strip() and len(line) - len(line.lstrip()) <= section_indent:
                        section = ""
                    continue
                key = entry_match.group(2).strip().strip("'\"")
                literal = entry_match.group(3).strip()
                if not literal or "{{" in literal:
                    continue
                if len(variables) >= MAX_VARIABLES:
                    raise ChartTemplateError(
                        f"Chart exceeds the {MAX_VARIABLES}-value limit before all "
                        "literal Secret values could be exposed safely."
                    )
                counter += 1
                variable_key = _safe_key(
                    f"{Path(path).stem}_{key}_{counter}"
                )
                values.setdefault("kubesightSecrets", {})[variable_key] = ""
                variables.append(
                    {
                        "key": variable_key,
                        "path": f"kubesightSecrets.{variable_key}",
                        "label": f"{Path(path).stem} {key}",
                        "description": "Literal Secret value removed during import.",
                        "type": "string",
                        "default": "",
                        "required": True,
                        "sensitive": True,
                        "category": "Secrets",
                    }
                )
                value_ref = f'.Values.kubesightSecrets.{variable_key}'
                expression = f'required "{key} is required" {value_ref}'
                if section == "data":
                    expression += " | b64enc"
                expression += " | quote"
                lines[line_index] = (
                    f"{entry_match.group(1)}{entry_match.group(2)}: "
                    "{{ " + expression + " }}"
                )
                if literal.startswith(("|", ">")):
                    entry_indent = len(entry_match.group(1))
                    for continuation in range(line_index + 1, len(lines)):
                        continuation_line = lines[continuation]
                        if (
                            continuation_line.strip()
                            and len(continuation_line) - len(continuation_line.lstrip())
                            <= entry_indent
                        ):
                            break
                        lines[continuation] = ""
                changed = True
                warnings.append(
                    f"Literal Secret value '{key}' in {path} was removed and made required."
                )
            documents[doc_index] = "\n".join(lines) + (
                "\n" if document.endswith("\n") else ""
            )
        if changed:
            files[path] = "".join(documents).encode("utf-8")
    return warnings


def _scrub_environment_values_files(files: Dict[str, bytes]) -> List[str]:
    """Keep ``values-<environment>.yaml`` files, minus any sensitive literals.

    Environment files are worth showing and reusing, but they routinely carry
    passwords, so every sensitive leaf is blanked before the file is stored.
    """
    warnings: List[str] = []
    for path in sorted(files):
        if not _is_environment_values_file(path):
            continue
        try:
            loaded = yaml.safe_load(files[path].decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            loaded = None
        if not isinstance(loaded, dict):
            files.pop(path, None)
            warnings.append(f"Excluded '{path}' because it is not a values mapping.")
            continue
        removed = 0
        for value_path, _ in list(_flatten_values(loaded)):
            if value_path and _is_sensitive_key_path(value_path):
                _set_nested(loaded, value_path, "")
                removed += 1
        if removed:
            # Only rewrite when something was actually blanked: an untouched file
            # keeps its original bytes, comments included.
            files[path] = yaml.safe_dump(loaded, sort_keys=False).encode("utf-8")
            warnings.append(
                f"Removed {removed} sensitive value{'' if removed == 1 else 's'} from '{path}'."
            )
    return warnings


def _prepare_existing_chart(
    chart_root: Path,
    *,
    payload: Dict[str, Any],
    source_type: str,
    source_ref: str,
) -> Dict[str, Any]:
    """Read a packaged chart into the keyword arguments ``_persist`` expects."""
    try:
        metadata = yaml.safe_load((chart_root / "Chart.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ChartTemplateError(f"Chart.yaml could not be read: {exc}") from exc
    values_path = chart_root / "values.yaml"
    try:
        values = yaml.safe_load(values_path.read_text(encoding="utf-8")) if values_path.exists() else {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ChartTemplateError(f"values.yaml could not be read: {exc}") from exc
    if not isinstance(values, dict):
        raise ChartTemplateError("values.yaml must contain a YAML object.")
    schema: Dict[str, Any] = {}
    schema_path = chart_root / "values.schema.json"
    if schema_path.exists():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            schema = {}

    scrubbed, variables, warnings = _scrub_chart_values(values, schema)
    _scrub_schema_secrets(schema)
    files = _read_chart_directory(chart_root)
    if not values_path.exists():
        warnings.append(
            "The chart has no values.yaml, so only the values you supply at deploy time apply."
        )
    if not any(path.startswith("templates/") for path in files):
        warnings.append("The chart has no templates/ directory and renders no resources.")
    warnings.extend(_scrub_environment_values_files(files))
    warnings.extend(_scrub_static_secret_templates(files, scrubbed, variables))
    if scrubbed != values or not values_path.exists():
        # Same rule as the environment files: a chart with nothing to scrub is
        # stored exactly as uploaded, so its values.yaml comments survive.
        files["values.yaml"] = yaml.safe_dump(scrubbed, sort_keys=False).encode("utf-8")
    if schema and "values.schema.json" in files:
        files["values.schema.json"] = json.dumps(schema, indent=2).encode("utf-8")
    name = _clean_text(payload.get("name"), 120) or _clean_text(metadata.get("name"), 120)
    if not name:
        raise ChartTemplateError("Chart.yaml does not contain a chart name.")
    return {
        "name": name,
        "description": _clean_text(payload.get("description"), 500)
        or _clean_text(metadata.get("description"), 500),
        "version": _clean_text(metadata.get("version"), 64) or "0.1.0",
        "app_version": _clean_text(metadata.get("appVersion"), 64),
        "source_type": source_type,
        "source_ref": source_ref,
        "files": files,
        "values": scrubbed,
        "variables": variables,
        "warnings": warnings,
        "resource_count": len([path for path in files if path.startswith("templates/")]),
    }


def _validate_git_url(raw_url: str) -> str:
    url = _clean_text(raw_url, 1000)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ChartTemplateError("Git repository URL must be a valid HTTPS URL.")
    if parsed.username or parsed.password:
        raise ChartTemplateError("Do not embed credentials in the Git URL.")
    if parsed.query or parsed.fragment:
        raise ChartTemplateError("Git repository URL cannot contain a query string or fragment.")
    return url


def _write_askpass(directory: str) -> str:
    path = os.path.join(directory, "kubesight-git-askpass.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "#!/bin/sh\n"
            'case \"$1\" in\n'
            '  *sername*) printf \"%s\" \"$KUBESIGHT_GIT_USERNAME\" ;;\n'
            '  *) printf \"%s\" \"$KUBESIGHT_GIT_TOKEN\" ;;\n'
            "esac\n"
        )
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def _git_environment(
    payload: Dict[str, Any], askpass_dir: str
) -> Tuple[Dict[str, str], str, str]:
    token = str(payload.get("token") or payload.get("accessToken") or "")
    username = _clean_text(payload.get("username"), 255) or "oauth2"
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": _write_askpass(askpass_dir),
            "KUBESIGHT_GIT_USERNAME": username,
            "KUBESIGHT_GIT_TOKEN": token,
        }
    )
    return env, token, username


def _redact_git_message(message: str, *secrets_to_redact: str) -> str:
    redacted = str(message or "")
    for secret in secrets_to_redact:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted[:1000]


def _git_clone(payload: Dict[str, Any], destination: str, askpass_dir: str) -> str:
    url = _validate_git_url(payload.get("repositoryUrl") or payload.get("url"))
    ref = _clean_text(payload.get("ref") or payload.get("branch"), 200)
    command = [
        "git",
        "-c",
        "credential.helper=",
        "clone",
        "--depth",
        "1",
        "--no-tags",
        "--single-branch",
        "--filter=blob:none",
    ]
    if ref:
        command.extend(["--branch", ref])
    command.extend(["--", url, destination])
    env, token, username = _git_environment(payload, askpass_dir)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ChartTemplateError("Git is not installed on the backend server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ChartTemplateError("Git clone timed out after 90 seconds.") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "Git clone failed.").strip()
        raise ChartTemplateError(_redact_git_message(message, token, username))
    return url


@contextmanager
def _private_import_directory() -> Iterable[str]:
    """Create a private, disposable Git workspace without tempfile's Windows ACL.

    Python 3.14's ``TemporaryDirectory`` uses a restrictive Windows creation mode
    that can make child creation fail under some service/sandbox identities. A
    random exclusive directory with inherited Windows ACLs avoids that issue;
    POSIX receives explicit owner-only permissions.
    """
    parent = Path(os.getenv("KUBESIGHT_TEMP_DIR") or os.getcwd()).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    path: Optional[Path] = None
    for _ in range(10):
        candidate = parent / f".kubesight-git-import-{secrets.token_hex(12)}"
        try:
            candidate.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            path = candidate
            break
        except FileExistsError:
            continue
    if path is None:
        raise ChartTemplateError("Unable to allocate a private Git import directory.")
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _resolve_subpath(root_dir: Path, requested: str, *, label: str) -> Path:
    relative = str(requested or "").replace("\\", "/").strip("/")
    if relative and any(part == ".." for part in relative.split("/")):
        raise ChartTemplateError(f"{label.capitalize()} path cannot contain '..'.")
    target = (root_dir / relative).resolve()
    root = root_dir.resolve()
    if target != root and root not in target.parents:
        raise ChartTemplateError(f"The selected path is outside the {label}.")
    if not target.exists():
        raise ChartTemplateError(f"The selected path does not exist in the {label}.")
    return target


def discover_git_refs(
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """List remote branches and tags without cloning repository contents."""
    try:
        with _private_import_directory() as temp_dir:
            url = _validate_git_url(payload.get("repositoryUrl") or payload.get("url"))
            env, token, username = _git_environment(payload, temp_dir)
            command = [
                "git",
                "-c",
                "credential.helper=",
                "ls-remote",
                "--symref",
                url,
                "HEAD",
                "refs/heads/*",
                "refs/tags/*",
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                    env=env,
                )
            except FileNotFoundError as exc:
                raise ChartTemplateError("Git is not installed on the backend server.") from exc
            except subprocess.TimeoutExpired as exc:
                raise ChartTemplateError(
                    "Git repository lookup timed out after 30 seconds."
                ) from exc
            if completed.returncode != 0:
                message = (
                    completed.stderr
                    or completed.stdout
                    or "Unable to read Git branches and tags."
                ).strip()
                raise ChartTemplateError(
                    _redact_git_message(message, token, username)
                )

            default_ref = ""
            refs: List[Dict[str, str]] = []
            seen = set()
            for line in completed.stdout.splitlines():
                if line.startswith("ref: "):
                    parts = line[5:].split("\t", 1)
                    if len(parts) == 2 and parts[1] == "HEAD":
                        default_ref = parts[0].removeprefix("refs/heads/")
                    continue
                parts = line.split("\t", 1)
                if len(parts) != 2:
                    continue
                full_name = parts[1]
                if full_name.endswith("^{}"):
                    continue
                if full_name.startswith("refs/heads/"):
                    ref_type = "branch"
                    name = full_name.removeprefix("refs/heads/")
                elif full_name.startswith("refs/tags/"):
                    ref_type = "tag"
                    name = full_name.removeprefix("refs/tags/")
                else:
                    continue
                key = (ref_type, name)
                if not name or key in seen:
                    continue
                seen.add(key)
                refs.append(
                    {
                        "name": name,
                        "type": ref_type,
                        "label": f"{name} ({ref_type})",
                    }
                )

            refs.sort(
                key=lambda item: (
                    item["type"] != "branch",
                    item["name"] != default_ref,
                    item["name"].lower(),
                )
            )
            if not refs:
                raise ChartTemplateError(
                    "The repository has no branches or tags to import."
                )
            if not default_ref:
                default_ref = refs[0]["name"]
            return {"refs": refs, "defaultRef": default_ref}, None, 200
    except ChartTemplateError as exc:
        return None, str(exc), 400
    except OSError as exc:
        return None, f"Unable to inspect the Git repository: {exc}", 400


def _list_git_repository_paths(repo_root: Path) -> Tuple[List[Dict[str, str]], bool]:
    paths: List[Dict[str, str]] = [
        {"path": "", "label": "/ (repository root)", "kind": "root"}
    ]
    truncated = False
    for current, directories, filenames in os.walk(repo_root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            directory
            for directory in directories
            if directory != ".git" and not (current_path / directory).is_symlink()
        )
        if current_path == repo_root:
            continue
        relative = current_path.relative_to(repo_root).as_posix()
        if "Chart.yaml" in filenames:
            kind = "chart"
            label = f"{relative}/ (Helm chart)"
        elif any(Path(filename).suffix.lower() in {".yaml", ".yml"} for filename in filenames):
            kind = "yaml"
            label = f"{relative}/ (contains YAML)"
        else:
            kind = "directory"
            label = f"{relative}/"
        paths.append({"path": relative, "label": label, "kind": kind})
        if len(paths) >= MAX_GIT_PATHS:
            truncated = True
            break
    return paths, truncated


def discover_git_paths(
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Shallow-clone a selected ref and return safe directory choices."""
    try:
        with _private_import_directory() as temp_dir:
            repo_root = Path(temp_dir) / "repository"
            _git_clone(payload, str(repo_root), temp_dir)
            paths, truncated = _list_git_repository_paths(repo_root)
            return {"paths": paths, "truncated": truncated}, None, 200
    except ChartTemplateError as exc:
        return None, str(exc), 400
    except OSError as exc:
        return None, f"Unable to inspect the Git repository: {exc}", 400


def _discover_chart_root(
    selected: Path, import_type: str, *, path_hint: str
) -> Optional[Path]:
    """Locate a packaged Helm chart under ``selected``, honouring the import type."""
    if (selected / "Chart.yaml").is_file():
        return selected
    chart_root: Optional[Path] = None
    if import_type in {"auto", "chart"}:
        candidates = [
            path.parent for path in selected.rglob("Chart.yaml") if ".git" not in path.parts
        ]
        if len(candidates) == 1:
            chart_root = candidates[0]
        elif len(candidates) > 1:
            raise ChartTemplateError(
                f"Multiple Helm charts were found. Specify the chart directory in {path_hint}."
            )
    if import_type == "chart" and not chart_root:
        raise ChartTemplateError(
            "No Chart.yaml was found. Specify the existing chart directory."
        )
    return chart_root


def _collect_yaml_source_files(selected: Path) -> List[Tuple[str, str]]:
    yaml_paths = [
        path
        for path in sorted(selected.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in {".yaml", ".yml"}
        and ".git" not in path.parts
    ]
    if not yaml_paths:
        raise ChartTemplateError("No Chart.yaml or Kubernetes YAML files were found.")
    source_files: List[Tuple[str, str]] = []
    total = 0
    for path in yaml_paths[:MAX_IMPORT_FILES]:
        content = path.read_text(encoding="utf-8")
        total += len(content.encode("utf-8"))
        if total > MAX_IMPORT_BYTES:
            raise ChartTemplateError("YAML content exceeds the 10 MiB import limit.")
        source_files.append((path.relative_to(selected).as_posix(), content))
    return source_files


def import_git_chart(
    payload: Dict[str, Any], actor_user_id: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    try:
        with _private_import_directory() as temp_dir:
            repo_root = Path(temp_dir) / "repository"
            url = _git_clone(payload, str(repo_root), temp_dir)
            selected = _resolve_subpath(
                repo_root, payload.get("path") or "", label="cloned repository"
            )
            import_type = _clean_text(payload.get("importType"), 20).lower() or "auto"
            chart_root = _discover_chart_root(
                selected, import_type, path_hint="Repository path"
            )
            ref = _clean_text(payload.get("ref") or payload.get("branch"), 200)
            repo_path = _clean_text(payload.get("path"), 500)
            source_ref = f"{url}{f'#{ref}' if ref else ''}{f':{repo_path}' if repo_path else ''}"
            if chart_root and import_type != "yaml":
                prepared = _prepare_existing_chart(
                    chart_root,
                    payload=payload,
                    source_type="git-chart",
                    source_ref=source_ref,
                )
                return _persist(**prepared, actor_user_id=actor_user_id), None, 201

            source_files = _collect_yaml_source_files(selected)
            name, files, values, variables, warnings, count = _convert_manifest_files(
                source_files, requested_name=_clean_text(payload.get("name"), 120)
            )
            return _persist(
                name=name,
                description=_clean_text(payload.get("description"), 500)
                or "Converted from Kubernetes YAML in Git",
                version="0.1.0",
                app_version="1.0.0",
                source_type="git-yaml",
                source_ref=source_ref,
                files=files,
                values=values,
                variables=variables,
                warnings=warnings,
                resource_count=count,
                actor_user_id=actor_user_id,
            ), None, 201
    except ChartTemplateError as exc:
        return None, str(exc), 400
    except (OSError, UnicodeError) as exc:
        return None, f"Unable to read the Git repository: {exc}", 400


def _decode_archive(payload: Dict[str, Any]) -> bytes:
    raw = payload.get("archiveBase64") or payload.get("archive_base64") or ""
    if not isinstance(raw, str) or not raw.strip():
        raise ChartTemplateError(f"An archive file ({ARCHIVE_SUFFIX_LABEL}) is required.")
    encoded = re.sub(r"\s+", "", raw)
    # Reject oversized uploads before decoding them into memory.
    if len(encoded) > (MAX_IMPORT_BYTES // 3 + 1) * 4:
        raise ChartTemplateError("The archive exceeds the 10 MiB import limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ChartTemplateError("The uploaded archive could not be decoded.") from exc
    if not data:
        raise ChartTemplateError("The uploaded archive is empty.")
    if len(data) > MAX_IMPORT_BYTES:
        raise ChartTemplateError("The archive exceeds the 10 MiB import limit.")
    return data


def _archive_member_path(name: str) -> Optional[str]:
    """Return a safe relative path for an archive entry, or None when skippable."""
    normalized = str(name or "").replace("\\", "/")
    if not normalized or normalized.endswith("/"):
        return None
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ChartTemplateError(f"The archive entry uses an absolute path: {name}")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ChartTemplateError(f"The archive entry escapes the archive root: {name}")
    if parts[-1] in ARCHIVE_JUNK_FILES or set(parts[:-1]) & ARCHIVE_JUNK_DIRS:
        return None
    return "/".join(parts)


class _ArchiveWriter:
    """Writes archive members to disk under a shared file-count and byte budget."""

    def __init__(self, destination: Path) -> None:
        self._destination = destination
        self._remaining = MAX_IMPORT_BYTES
        self.written = 0

    def add(self, relative: str, declared_size: int, handle: Any) -> None:
        self.written += 1
        if self.written > MAX_IMPORT_FILES:
            raise ChartTemplateError(f"Archives may contain at most {MAX_IMPORT_FILES} files.")
        if declared_size > self._remaining:
            raise ChartTemplateError("Archive content exceeds the 10 MiB import limit.")
        # Read one byte past the remaining budget so a header that understates
        # a member's size cannot smuggle a decompression bomb past the limit.
        content = handle.read(self._remaining + 1)
        if len(content) > self._remaining:
            raise ChartTemplateError("Archive content exceeds the 10 MiB import limit.")
        self._remaining -= len(content)
        target = self._destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _extract_zip_archive(data: bytes, writer: _ArchiveWriter) -> None:
    try:
        opened = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ChartTemplateError("The uploaded file is not a valid .zip archive.") from exc
    with opened as archive:
        for info in archive.infolist():
            if info.flag_bits & 0x1:
                raise ChartTemplateError("Password-protected archives are not supported.")
            if info.is_dir():
                continue
            relative = _archive_member_path(info.filename)
            if relative is None:
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ChartTemplateError(
                    f"The archive contains a symbolic link, which is not supported: {relative}"
                )
            try:
                with archive.open(info) as handle:
                    writer.add(relative, info.file_size, handle)
            except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
                raise ChartTemplateError(
                    f"The archive entry could not be read: {relative}"
                ) from exc


def _extract_tar_archive(data: bytes, writer: _ArchiveWriter) -> None:
    try:
        opened = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ChartTemplateError(
            "The uploaded file is not a valid .tgz or .tar.gz archive."
        ) from exc
    with opened as archive:
        for member in archive:
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                raise ChartTemplateError(
                    f"The archive contains a link, which is not supported: {member.name}"
                )
            if not member.isfile():
                # Devices, FIFOs and sockets never belong in a chart.
                continue
            relative = _archive_member_path(member.name)
            if relative is None:
                continue
            try:
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                with handle:
                    writer.add(relative, member.size, handle)
            except (tarfile.TarError, EOFError) as exc:
                raise ChartTemplateError(
                    f"The archive entry could not be read: {relative}"
                ) from exc


def _extract_archive(data: bytes, filename: str, destination: Path) -> None:
    """Safely extract a .zip, .tgz or .tar.gz upload into ``destination``."""
    writer = _ArchiveWriter(destination)
    if data[:2] == b"PK":
        _extract_zip_archive(data, writer)
    elif data[:2] == b"\x1f\x8b":
        _extract_tar_archive(data, writer)
    elif filename.lower().endswith(".zip"):
        # Reuse zipfile's own diagnostics for a truncated or corrupt .zip.
        _extract_zip_archive(data, writer)
    else:
        raise ChartTemplateError(
            f"The uploaded file is not a recognised archive ({ARCHIVE_SUFFIX_LABEL})."
        )
    if not writer.written:
        raise ChartTemplateError("The archive does not contain any importable files.")


def _prepare_archive_import(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract an uploaded archive into the keyword arguments ``_persist`` expects."""
    filename = _clean_text(payload.get("filename") or payload.get("fileName"), 255)
    if filename and not filename.lower().endswith(ARCHIVE_SUFFIXES):
        raise ChartTemplateError(f"Only {ARCHIVE_SUFFIX_LABEL} archives are supported.")
    data = _decode_archive(payload)
    with _private_import_directory() as temp_dir:
        archive_root = Path(temp_dir) / "archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        _extract_archive(data, filename, archive_root)
        selected = _resolve_subpath(archive_root, payload.get("path") or "", label="archive")
        import_type = _clean_text(payload.get("importType"), 20).lower() or "auto"
        chart_root = _discover_chart_root(
            selected, import_type, path_hint="Path inside archive"
        )
        archive_path = _clean_text(payload.get("path"), 500)
        source_ref = (
            f"{filename or 'uploaded-archive'}"
            f"{f':{archive_path}' if archive_path else ''}"
        )
        if chart_root and import_type != "yaml":
            return _prepare_existing_chart(
                chart_root,
                payload=payload,
                source_type="archive-chart",
                source_ref=source_ref,
            )

        source_files = _collect_yaml_source_files(selected)
        name, files, values, variables, warnings, count = _convert_manifest_files(
            source_files, requested_name=_clean_text(payload.get("name"), 120)
        )
        return {
            "name": name,
            "description": _clean_text(payload.get("description"), 500)
            or "Converted from Kubernetes YAML in an uploaded archive",
            "version": "0.1.0",
            "app_version": "1.0.0",
            "source_type": "archive-yaml",
            "source_ref": source_ref,
            "files": files,
            "values": values,
            "variables": variables,
            "warnings": warnings,
            "resource_count": count,
        }


def import_archive_chart(
    payload: Dict[str, Any], actor_user_id: Optional[int] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Import a chart or raw Kubernetes manifests from an uploaded archive."""
    try:
        prepared = _prepare_archive_import(payload)
        return _persist(**prepared, actor_user_id=actor_user_id), None, 201
    except ChartTemplateError as exc:
        return None, str(exc), 400
    except (OSError, UnicodeError) as exc:
        return None, f"Unable to read the archive: {exc}", 400


def chart_archive_base64(slug: str, version: str = "") -> str:
    row = _get_row(slug)
    files = _decode_files(_definition_for(row, version))
    root = _slugify(row.name)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for relative, content in sorted(files.items()):
            info = tarfile.TarInfo(name=f"{root}/{relative}")
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _coerce_value(value: Any, variable: Dict[str, Any]) -> Any:
    value_type = variable.get("type")
    if value_type == "integer":
        return int(value)
    if value_type == "number":
        return float(value)
    if value_type == "boolean":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ValueError("must be true or false")
    if value_type in {"array", "object"}:
        empty: Any = [] if value_type == "array" else {}
        if value is None:
            return empty
        if isinstance(value, str):
            # The form edits these as YAML/JSON text; an emptied field means an
            # empty collection.
            parsed = yaml.safe_load(value) if value.strip() else empty
        else:
            # Already structured (an untouched chart default): never str() it,
            # or the rendered values.yaml gets a Python repr instead of a list.
            parsed = value
        if value_type == "array" and not isinstance(parsed, list):
            raise ValueError("must be a YAML list, for example: - name: my-secret")
        if value_type == "object" and not isinstance(parsed, dict):
            raise ValueError("must be a YAML object, for example: key: value")
        return parsed
    return "" if value is None else str(value)


def build_values_yaml(
    slug: str,
    answers: Dict[str, Any],
    version: str = "",
    values_file: str = "",
) -> str:
    row = _get_row(slug)
    definition = _definition_for(row, version)
    values = deepcopy(definition.get("values") or {})
    selected_values_file = _clean_text(values_file, 500)
    if selected_values_file:
        environment_values = _environment_values_files(definition)
        if selected_values_file not in environment_values:
            raise ChartTemplateError(
                f"Environment values file '{selected_values_file}' is not available for this chart version."
            )
        values = _merge_values(values, environment_values[selected_values_file])
    variables = definition.get("variables") or []
    answer_map = answers if isinstance(answers, dict) else {}
    for variable in variables:
        path = str(variable.get("path") or "")
        if not path:
            continue
        key = variable.get("key")
        if path in answer_map:
            raw = answer_map[path]
        elif key in answer_map:
            raw = answer_map[key]
        else:
            # Base defaults are already in ``values`` and a selected environment
            # file has already been merged over them. Reapplying each variable's
            # base default here would erase the selected environment.
            raw = _get_nested(values, path.split("."), variable.get("default"))
            if variable.get("required") and (raw is None or str(raw).strip() == ""):
                raise ChartTemplateError(f"{variable.get('label') or path} is required.")
            continue
        if variable.get("required") and (raw is None or str(raw).strip() == ""):
            raise ChartTemplateError(f"{variable.get('label') or path} is required.")
        if (
            not variable.get("required")
            and (raw is None or str(raw).strip() == "")
            and variable.get("default") in (None, "")
        ):
            # Preserve an intentionally empty/null optional chart default. This
            # matters for schema-typed optional numbers and objects, where
            # coercing an untouched empty form field would otherwise fail.
            continue
        try:
            coerced = _coerce_value(raw, variable)
        except (TypeError, ValueError) as exc:
            raise ChartTemplateError(
                f"{variable.get('label') or path} {str(exc)}."
            ) from exc
        _set_nested(values, path.split("."), coerced)
    return yaml.safe_dump(values, sort_keys=False, default_flow_style=False)


def prepare_chart_template_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = str(payload.get("chartSource") or payload.get("chart_source") or "").lower()
    if source != "template":
        return payload
    slug = _clean_text(
        payload.get("chartTemplateId") or payload.get("chart_template_id"), 120
    )
    if not slug:
        raise ChartTemplateError("chartTemplateId is required.")
    row = _get_row(slug)
    # A chart can hold several uploaded versions; deploy the requested one and
    # fall back to whichever version is currently selected.
    version = _clean_text(
        payload.get("chartVersion") or payload.get("chart_version"), 64
    ) or (row.version or "")
    _definition_for(row, version)
    prepared = dict(payload)
    values_file = _clean_text(
        payload.get("valuesFile") or payload.get("values_file"), 500
    )
    prepared.update(
        {
            "chartSource": "local",
            "chartArchiveBase64": chart_archive_base64(slug, version),
            "valuesYaml": build_values_yaml(
                slug,
                payload.get("values") or {},
                version,
                values_file,
            ),
            "chartName": row.name,
            "chartVersion": version or row.version,
            "valuesFile": values_file,
        }
    )
    return prepared
