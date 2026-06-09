"""Application deployment version history and rollback."""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..access_engine import can_access_namespace, is_admin
from ..audit import log_audit
from ..db import db
from ..models import ApplicationDeploymentVersion, AppCatalogEntry, User
from .app_catalog_service import get_entry_for_inventory
from .deployment_service import apply_yaml
from .inventory_service import parse_inventory_id


def _serialize_version(entry: ApplicationDeploymentVersion, include_yaml: bool = False) -> Dict[str, Any]:
    created_by = entry.created_by
    return {
        "id": entry.id,
        "versionLabel": entry.version_label,
        "versionMajor": entry.version_major,
        "versionMinor": entry.version_minor,
        "clusterId": entry.cluster_id,
        "namespace": entry.namespace,
        "appName": entry.app_name,
        "workloadType": entry.workload_type,
        "changeSummary": entry.change_summary,
        "createdBy": created_by.username if created_by else None,
        "createdByUserId": entry.created_by_user_id,
        "createdAt": entry.created_at.isoformat() if entry.created_at else None,
        "catalogEntryId": entry.catalog_entry_id,
        **({"yaml": entry.yaml_snapshot} if include_yaml else {}),
    }


def _next_version(cluster_id: str, namespace: str, app_name: str) -> Tuple[str, int, int]:
    latest = (
        ApplicationDeploymentVersion.query.filter_by(
            cluster_id=cluster_id,
            namespace=namespace,
            app_name=app_name,
        )
        .order_by(ApplicationDeploymentVersion.version_major.desc(), ApplicationDeploymentVersion.version_minor.desc())
        .first()
    )
    if not latest:
        return "v1.0", 1, 0
    minor = latest.version_minor + 1
    major = latest.version_major
    if minor >= 10:
        major += 1
        minor = 0
    return f"v{major}.{minor}", major, minor


def create_deployment_version(
    user: Optional[User],
    *,
    cluster_id: str,
    namespace: str,
    app_name: str,
    workload_type: str,
    yaml_snapshot: str,
    change_summary: Optional[str] = None,
    wizard_config: Optional[Dict[str, Any]] = None,
    catalog_entry_id: Optional[int] = None,
) -> ApplicationDeploymentVersion:
    version_label, major, minor = _next_version(cluster_id, namespace, app_name)
    entry = ApplicationDeploymentVersion(
        catalog_entry_id=catalog_entry_id,
        cluster_id=cluster_id,
        namespace=namespace,
        app_name=app_name,
        version_label=version_label,
        version_major=major,
        version_minor=minor,
        workload_type=workload_type,
        change_summary=change_summary or f"Deployed {workload_type} {app_name}",
        yaml_snapshot=yaml_snapshot,
        wizard_config=wizard_config,
        created_by_user_id=user.id if user else None,
    )
    db.session.add(entry)
    db.session.commit()
    log_audit(
        "application_version_created",
        actor=user,
        target_type="application_version",
        target_id=str(entry.id),
        details={"version": version_label, "app": app_name, "namespace": namespace, "cluster": cluster_id},
    )
    return entry


def list_versions_for_inventory(
    user: Optional[User],
    inventory_id: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], int]:
    parsed = parse_inventory_id(inventory_id)
    if not parsed:
        return None, "Invalid inventory id", 400
    cluster_id, namespace, app_name = parsed

    if user and not is_admin(user) and not can_access_namespace(user, cluster_id, namespace):
        return None, "Forbidden", 403

    versions = (
        ApplicationDeploymentVersion.query.filter_by(
            cluster_id=cluster_id,
            namespace=namespace,
            app_name=app_name,
        )
        .order_by(ApplicationDeploymentVersion.created_at.desc())
        .all()
    )
    return [_serialize_version(v) for v in versions], None, 200


def get_version(
    user: Optional[User],
    version_id: int,
    *,
    include_yaml: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    entry = ApplicationDeploymentVersion.query.get(version_id)
    if not entry:
        return None, "Version not found", 404
    if user and not is_admin(user) and not can_access_namespace(user, entry.cluster_id, entry.namespace):
        return None, "Forbidden", 403
    return _serialize_version(entry, include_yaml=include_yaml), None, 200


def compare_versions(
    user: Optional[User],
    version_id_a: int,
    version_id_b: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    a = ApplicationDeploymentVersion.query.get(version_id_a)
    b = ApplicationDeploymentVersion.query.get(version_id_b)
    if not a or not b:
        return None, "One or both versions not found", 404
    if user and not is_admin(user):
        if not can_access_namespace(user, a.cluster_id, a.namespace):
            return None, "Forbidden", 403

    diff_lines = list(
        difflib.unified_diff(
            a.yaml_snapshot.splitlines(),
            b.yaml_snapshot.splitlines(),
            fromfile=a.version_label,
            tofile=b.version_label,
            lineterm="",
        )
    )
    return {
        "versionA": _serialize_version(a),
        "versionB": _serialize_version(b),
        "diff": "\n".join(diff_lines),
    }, None, 200


def rollback_to_version(
    user: Optional[User],
    version_id: int,
    confirmation: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    entry = ApplicationDeploymentVersion.query.get(version_id)
    if not entry:
        return None, "Version not found", 404
    if user and not is_admin(user) and not can_access_namespace(user, entry.cluster_id, entry.namespace):
        return None, "Forbidden", 403

    expected = f"APPLY {entry.namespace}"
    if confirmation.strip() != expected:
        return None, f'Confirmation must be exactly "{expected}"', 400

    data, error, status = apply_yaml(
        user,
        entry.cluster_id,
        entry.namespace,
        entry.yaml_snapshot,
        confirmation,
    )
    if error:
        return None, error, status

    catalog = get_entry_for_inventory(entry.cluster_id, entry.namespace, entry.app_name)
    new_version = create_deployment_version(
        user,
        cluster_id=entry.cluster_id,
        namespace=entry.namespace,
        app_name=entry.app_name,
        workload_type=entry.workload_type or "Deployment",
        yaml_snapshot=entry.yaml_snapshot,
        change_summary=f"Rollback to {entry.version_label}",
        wizard_config=entry.wizard_config,
        catalog_entry_id=catalog.id if catalog else entry.catalog_entry_id,
    )

    log_audit(
        "application_version_rollback",
        actor=user,
        target_type="application_version",
        target_id=str(version_id),
        details={"rolledBackTo": entry.version_label, "newVersion": new_version.version_label},
    )

    return {
        "rollbackFrom": entry.version_label,
        "newVersion": _serialize_version(new_version),
        "applyResult": data,
    }, None, 200
