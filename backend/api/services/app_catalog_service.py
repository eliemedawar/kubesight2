"""Application catalog CRUD — metadata only, does not mutate Kubernetes resources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..access_engine import can_access_namespace, is_admin, user_has_permission
from ..audit import log_audit
from ..db import db
from ..models import AppCatalogEntry, User
from .inventory_service import make_inventory_id


def _serialize_entry(entry: AppCatalogEntry) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "clusterId": entry.cluster_id,
        "cluster_id": entry.cluster_id,
        "namespace": entry.namespace,
        "workloadType": entry.workload_type,
        "workload_type": entry.workload_type,
        "workloadName": entry.workload_name,
        "workload_name": entry.workload_name,
        "displayName": entry.display_name,
        "display_name": entry.display_name,
        "ownerTeam": entry.owner_team or "Unassigned",
        "owner_team": entry.owner_team,
        "environment": entry.environment or "Not set",
        "criticality": entry.criticality or "Not set",
        "description": entry.description,
        "documentationUrl": entry.documentation_url,
        "documentation_url": entry.documentation_url,
        "contactEmail": entry.contact_email or "Not set",
        "contact_email": entry.contact_email,
        "tags": entry.tags or [],
        "source": entry.source,
        "createdByUserId": entry.created_by_user_id,
        "createdAt": entry.created_at.isoformat() if entry.created_at else None,
        "updatedAt": entry.updated_at.isoformat() if entry.updated_at else None,
        "isActive": entry.is_active,
        "inventoryId": make_inventory_id(entry.cluster_id, entry.namespace, entry.display_name),
    }


def _can_manage_catalog(user: Optional[User], cluster_id: str, namespace: str) -> bool:
    if not user:
        return True
    if is_admin(user):
        return True
    return can_access_namespace(user, cluster_id, namespace)


def list_active_entries(
    cluster_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> List[AppCatalogEntry]:
    query = AppCatalogEntry.query.filter_by(is_active=True)
    if cluster_id:
        query = query.filter_by(cluster_id=cluster_id)
    if namespace:
        query = query.filter_by(namespace=namespace)
    return query.all()


def get_entry_by_id(entry_id: int) -> Optional[AppCatalogEntry]:
    return AppCatalogEntry.query.get(entry_id)


def get_entry_for_inventory(
    cluster_id: str,
    namespace: str,
    app_name: str,
    workload_name: Optional[str] = None,
) -> Optional[AppCatalogEntry]:
    """Find active catalog entry matching discovered or registered app."""
    entries = AppCatalogEntry.query.filter_by(
        cluster_id=cluster_id,
        namespace=namespace,
        is_active=True,
    ).all()
    for entry in entries:
        if entry.display_name == app_name:
            return entry
        if workload_name and entry.workload_name == workload_name:
            return entry
    return None


def register_existing_app(
    user: Optional[User],
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "inventory:register"):
        log_audit(
            "unauthorized_inventory_attempt",
            actor=user,
            target_type="inventory",
            details={"action": "register", "reason": "missing permission"},
        )
        return None, "Forbidden", 403

    cluster_id = (payload.get("clusterId") or payload.get("cluster_id") or "").strip()
    namespace = (payload.get("namespace") or "").strip()
    workload_type = (payload.get("workloadType") or payload.get("workload_type") or "").strip()
    workload_name = (payload.get("workloadName") or payload.get("workload_name") or "").strip()
    display_name = (payload.get("displayName") or payload.get("display_name") or workload_name).strip()

    if not all([cluster_id, namespace, workload_type, workload_name, display_name]):
        return None, "clusterId, namespace, workloadType, workloadName, and displayName are required", 400

    if user and not _can_manage_catalog(user, cluster_id, namespace):
        log_audit(
            "unauthorized_inventory_attempt",
            actor=user,
            target_type="namespace",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": "register"},
        )
        return None, "Forbidden", 403

    existing = AppCatalogEntry.query.filter_by(
        cluster_id=cluster_id,
        namespace=namespace,
        workload_name=workload_name,
        is_active=True,
    ).first()
    if existing:
        return None, "An active catalog entry already exists for this workload", 409

    entry = AppCatalogEntry(
        cluster_id=cluster_id,
        namespace=namespace,
        workload_type=workload_type,
        workload_name=workload_name,
        display_name=display_name,
        owner_team=(payload.get("ownerTeam") or payload.get("owner_team") or "").strip() or None,
        environment=(payload.get("environment") or "").strip() or None,
        criticality=(payload.get("criticality") or "").strip() or None,
        description=(payload.get("description") or "").strip() or None,
        documentation_url=(payload.get("documentationUrl") or payload.get("documentation_url") or "").strip() or None,
        contact_email=(payload.get("contactEmail") or payload.get("contact_email") or "").strip() or None,
        tags=payload.get("tags") or [],
        source="Registered",
        created_by_user_id=user.id if user else None,
        is_active=True,
    )
    db.session.add(entry)
    db.session.commit()

    log_audit(
        "app_registered",
        actor=user,
        target_type="app_catalog",
        target_id=str(entry.id),
        details={
            "cluster": cluster_id,
            "namespace": namespace,
            "workload_name": workload_name,
            "display_name": display_name,
            "result": "success",
        },
    )
    return _serialize_entry(entry), None, 201


def update_catalog_entry(
    user: Optional[User],
    entry_id: int,
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "inventory:update"):
        log_audit(
            "unauthorized_inventory_attempt",
            actor=user,
            target_type="inventory",
            details={"action": "update", "entry_id": entry_id},
        )
        return None, "Forbidden", 403

    entry = get_entry_by_id(entry_id)
    if not entry or not entry.is_active:
        return None, "Catalog entry not found", 404

    if user and not _can_manage_catalog(user, entry.cluster_id, entry.namespace):
        return None, "Forbidden", 403

    field_map = {
        "displayName": "display_name",
        "display_name": "display_name",
        "ownerTeam": "owner_team",
        "owner_team": "owner_team",
        "environment": "environment",
        "criticality": "criticality",
        "description": "description",
        "documentationUrl": "documentation_url",
        "documentation_url": "documentation_url",
        "contactEmail": "contact_email",
        "contact_email": "contact_email",
    }
    for key, attr in field_map.items():
        if key in payload:
            value = payload[key]
            setattr(entry, attr, (value.strip() if isinstance(value, str) else value) or None)
    if "tags" in payload:
        entry.tags = payload["tags"] or []

    entry.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    log_audit(
        "app_metadata_updated",
        actor=user,
        target_type="app_catalog",
        target_id=str(entry.id),
        details={
            "cluster": entry.cluster_id,
            "namespace": entry.namespace,
            "display_name": entry.display_name,
            "result": "success",
        },
    )
    return _serialize_entry(entry), None, 200


def remove_from_inventory(
    user: Optional[User],
    entry_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "inventory:remove"):
        log_audit(
            "unauthorized_inventory_attempt",
            actor=user,
            target_type="inventory",
            details={"action": "remove", "entry_id": entry_id},
        )
        return None, "Forbidden", 403

    entry = get_entry_by_id(entry_id)
    if not entry or not entry.is_active:
        return None, "Catalog entry not found", 404

    if user and not _can_manage_catalog(user, entry.cluster_id, entry.namespace):
        return None, "Forbidden", 403

    entry.is_active = False
    entry.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    log_audit(
        "app_removed_from_inventory",
        actor=user,
        target_type="app_catalog",
        target_id=str(entry.id),
        details={
            "cluster": entry.cluster_id,
            "namespace": entry.namespace,
            "display_name": entry.display_name,
            "result": "success",
            "note": "metadata only — Kubernetes resources unchanged",
        },
    )
    return {"id": entry_id, "removed": True}, None, 200


def create_or_update_from_deployment(
    user: Optional[User],
    *,
    cluster_id: str,
    namespace: str,
    display_name: str,
    workload_type: str = "Deployment",
    workload_name: Optional[str] = None,
    owner_team: Optional[str] = None,
    environment: Optional[str] = None,
    criticality: Optional[str] = None,
    description: Optional[str] = None,
    contact_email: Optional[str] = None,
    documentation_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> AppCatalogEntry:
    """Create or refresh catalog entry after a KubeSight deployment."""
    entry = AppCatalogEntry.query.filter_by(
        cluster_id=cluster_id,
        namespace=namespace,
        display_name=display_name,
        is_active=True,
    ).first()
    if not entry and workload_name:
        entry = AppCatalogEntry.query.filter_by(
            cluster_id=cluster_id,
            namespace=namespace,
            workload_name=workload_name,
            is_active=True,
        ).first()

    if entry:
        entry.workload_type = workload_type
        entry.workload_name = workload_name or entry.workload_name
        entry.source = "Deployed by KubeSight"
        if owner_team:
            entry.owner_team = owner_team
        if environment:
            entry.environment = environment
        if criticality:
            entry.criticality = criticality
        if description:
            entry.description = description
        if contact_email:
            entry.contact_email = contact_email
        if documentation_url:
            entry.documentation_url = documentation_url
        if tags:
            entry.tags = tags
        entry.updated_at = datetime.now(timezone.utc)
    else:
        entry = AppCatalogEntry(
            cluster_id=cluster_id,
            namespace=namespace,
            workload_type=workload_type,
            workload_name=workload_name,
            display_name=display_name,
            owner_team=owner_team,
            environment=environment,
            criticality=criticality,
            description=description,
            contact_email=contact_email,
            documentation_url=documentation_url,
            tags=tags or [],
            source="Deployed by KubeSight",
            created_by_user_id=user.id if user else None,
            is_active=True,
        )
        db.session.add(entry)

    db.session.commit()
    return entry


def create_or_update_from_helm(
    user: Optional[User],
    *,
    cluster_id: str,
    namespace: str,
    release_name: str,
    chart_name: Optional[str] = None,
    chart_version: Optional[str] = None,
    app_version: Optional[str] = None,
    helm_revision: Optional[int] = None,
    owner_team: Optional[str] = None,
    environment: Optional[str] = None,
    criticality: Optional[str] = None,
    description: Optional[str] = None,
) -> AppCatalogEntry:
    display_name = release_name
    entry = AppCatalogEntry.query.filter_by(
        cluster_id=cluster_id,
        namespace=namespace,
        release_name=release_name,
        is_active=True,
    ).first()
    if not entry:
        entry = AppCatalogEntry.query.filter_by(
            cluster_id=cluster_id,
            namespace=namespace,
            display_name=display_name,
            is_active=True,
        ).first()

    if entry:
        entry.source = "Helm"
        entry.release_name = release_name
        entry.display_name = display_name
        entry.workload_type = "Helm Release"
        entry.workload_name = release_name
        if chart_name:
            entry.chart_name = chart_name
        if chart_version:
            entry.chart_version = chart_version
        if app_version:
            entry.app_version = app_version
        if helm_revision is not None:
            entry.helm_revision = helm_revision
        if owner_team:
            entry.owner_team = owner_team
        if environment:
            entry.environment = environment
        if criticality:
            entry.criticality = criticality
        if description:
            entry.description = description
        entry.updated_at = datetime.now(timezone.utc)
    else:
        entry = AppCatalogEntry(
            cluster_id=cluster_id,
            namespace=namespace,
            display_name=display_name,
            release_name=release_name,
            workload_type="Helm Release",
            workload_name=release_name,
            chart_name=chart_name,
            chart_version=chart_version,
            app_version=app_version,
            helm_revision=helm_revision,
            owner_team=owner_team,
            environment=environment,
            criticality=criticality,
            description=description,
            source="Helm",
            created_by_user_id=user.id if user else None,
            is_active=True,
        )
        db.session.add(entry)

    db.session.commit()
    return entry
