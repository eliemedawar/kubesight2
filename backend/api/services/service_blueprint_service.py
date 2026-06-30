"""Service Catalog — CRUD for reusable service blueprints.

A blueprint captures the *logical* architecture of a business service: logical
components (Frontend, Backend, Database, ...), logical connections between them,
and the requirements/smart-defaults needed to deploy it. Nothing here references
a real Kubernetes object name — that mapping happens at Deploy From Blueprint
time (see ``blueprint_deploy_service``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import (
    ServiceBlueprint,
    ServiceBlueprintComponent,
    ServiceBlueprintConnection,
    ServiceBlueprintRequirement,
)

VALID_STATUSES = ("draft", "ready", "deprecated")
VALID_CRITICALITIES = ("low", "medium", "high", "critical")
VALID_COMPONENT_TYPES = (
    "deployment", "statefulset", "daemonset", "cronjob", "service", "ingress",
    "database", "redis", "kafka", "worker", "external_service", "cache", "queue",
)
VALID_REQUIREMENT_TYPES = (
    "env_var", "secret", "configmap", "pvc", "ingress_host", "tls_secret",
    "image_pull_secret", "hpa", "resource_limit", "database_credential",
    "external_endpoint",
)
VALID_VALUE_SOURCES = (
    "manual", "dropdown", "existing_secret", "existing_configmap", "generated",
    "blueprint_default", "detected_from_cluster",
)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _clean_str(value: Any, max_len: Optional[int] = None) -> Optional[str]:
    text = (str(value).strip() if value is not None else "")
    if not text:
        return None
    return text[:max_len] if max_len else text


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _coerce_json(value: Any) -> Optional[Any]:
    """Pass dict/list JSON values through; treat everything else as absent."""
    if isinstance(value, (dict, list)):
        return value
    return None


# ---------------------------------------------------------------------------
# Serializers (camelCase for the frontend)
# ---------------------------------------------------------------------------

def _component_to_dict(c: ServiceBlueprintComponent) -> Dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "role": c.role or "",
        "componentType": c.component_type,
        "required": bool(c.required),
        "supportsExternal": bool(c.supports_external),
        "defaultTemplateId": c.default_template_id,
        "description": c.description or "",
        "configSchema": c.config_schema,
        "defaultValues": c.default_values,
        "validationRules": c.validation_rules,
        "defaultPort": c.default_port,
        "defaultResources": c.default_resources,
        "defaultHealth": c.default_health,
        "defaultHpa": c.default_hpa,
        "positionX": c.position_x,
        "positionY": c.position_y,
        "position": c.position,
    }


def _connection_to_dict(cn: ServiceBlueprintConnection) -> Dict[str, Any]:
    return {
        "id": cn.id,
        "sourceComponentId": cn.source_component_id,
        "targetComponentId": cn.target_component_id,
        "connectionType": cn.connection_type,
        "protocol": cn.protocol,
        "port": cn.port,
        "description": cn.description or "",
    }


def _requirement_to_dict(r: ServiceBlueprintRequirement) -> Dict[str, Any]:
    return {
        "id": r.id,
        "componentId": r.component_id,
        "key": r.key,
        "requirementType": r.requirement_type,
        "required": bool(r.required),
        "defaultValue": r.default_value,
        "allowedValues": r.allowed_values,
        "valueSource": r.value_source,
        "secret": bool(r.secret),
        "autoGenerate": bool(r.auto_generate),
        "description": r.description or "",
    }


def _blueprint_summary(bp: ServiceBlueprint) -> Dict[str, Any]:
    return {
        "id": bp.id,
        "name": bp.name,
        "description": bp.description or "",
        "category": bp.category or "",
        "ownerTeam": bp.owner_team or "",
        "criticality": bp.criticality or "",
        "status": bp.status,
        "version": bp.version,
        "componentCount": len(bp.components),
        "dependencyCount": len(bp.connections),
        "requirementCount": len(bp.requirements),
        "appServiceCount": bp.app_services.count(),
        "createdAt": bp.created_at.isoformat() if bp.created_at else None,
        "updatedAt": bp.updated_at.isoformat() if bp.updated_at else None,
    }


def _blueprint_to_dict(bp: ServiceBlueprint) -> Dict[str, Any]:
    components = sorted(bp.components, key=lambda c: (c.position, c.id))
    return {
        **_blueprint_summary(bp),
        "components": [_component_to_dict(c) for c in components],
        "connections": [_connection_to_dict(cn) for cn in sorted(bp.connections, key=lambda x: x.id)],
        "requirements": [_requirement_to_dict(r) for r in sorted(bp.requirements, key=lambda x: x.id)],
    }


# ---------------------------------------------------------------------------
# Child persistence (replace-all, temp-id mapping like the topology editor)
# ---------------------------------------------------------------------------

def _save_children(blueprint: ServiceBlueprint, payload: Dict[str, Any]) -> None:
    """Replace components/connections/requirements from the payload.

    Components carry a client ``tempId`` (or their existing ``id``); connections
    and requirements reference components via ``sourceTempId``/``targetTempId``/
    ``componentTempId`` (falling back to the numeric ids). All children are
    rebuilt so edits, reorders and deletions are handled uniformly.
    """
    # Children must be cleared before nodes due to FK constraints.
    ServiceBlueprintConnection.query.filter_by(blueprint_id=blueprint.id).delete()
    ServiceBlueprintRequirement.query.filter_by(blueprint_id=blueprint.id).delete()
    ServiceBlueprintComponent.query.filter_by(blueprint_id=blueprint.id).delete()
    db.session.flush()

    components_raw = payload.get("components") or []
    connections_raw = payload.get("connections") or []
    requirements_raw = payload.get("requirements") or []

    temp_to_id: Dict[str, int] = {}
    for index, data in enumerate(components_raw):
        name = _clean_str(data.get("name"), 120)
        if not name:
            continue
        component_type = (data.get("componentType") or "deployment").strip()
        if component_type not in VALID_COMPONENT_TYPES:
            component_type = "deployment"
        component = ServiceBlueprintComponent(
            blueprint_id=blueprint.id,
            name=name,
            role=_clean_str(data.get("role"), 120),
            component_type=component_type,
            required=_coerce_bool(data.get("required"), True),
            supports_external=_coerce_bool(data.get("supportsExternal"), False),
            default_template_id=_clean_str(data.get("defaultTemplateId"), 120),
            description=_clean_str(data.get("description")),
            config_schema=_coerce_json(data.get("configSchema")),
            default_values=_coerce_json(data.get("defaultValues")),
            validation_rules=_coerce_json(data.get("validationRules")),
            default_port=_coerce_int(data.get("defaultPort")),
            default_resources=_coerce_json(data.get("defaultResources")),
            default_health=_coerce_json(data.get("defaultHealth")),
            default_hpa=_coerce_json(data.get("defaultHpa")),
            position_x=_coerce_float(data.get("positionX")),
            position_y=_coerce_float(data.get("positionY")),
            position=_coerce_int(data.get("position")) if data.get("position") is not None else index,
        )
        db.session.add(component)
        db.session.flush()
        for key in (data.get("tempId"), data.get("id")):
            if key is not None and str(key) != "":
                temp_to_id[str(key)] = component.id

    def _resolve(ref_value: Any) -> Optional[int]:
        if ref_value is None:
            return None
        return temp_to_id.get(str(ref_value))

    seen_edges: set = set()
    for data in connections_raw:
        src = _resolve(data.get("sourceTempId") if data.get("sourceTempId") is not None else data.get("sourceComponentId"))
        tgt = _resolve(data.get("targetTempId") if data.get("targetTempId") is not None else data.get("targetComponentId"))
        if not src or not tgt or src == tgt:
            continue
        if (src, tgt) in seen_edges:
            continue
        seen_edges.add((src, tgt))
        db.session.add(ServiceBlueprintConnection(
            blueprint_id=blueprint.id,
            source_component_id=src,
            target_component_id=tgt,
            connection_type=_clean_str(data.get("connectionType"), 32),
            protocol=_clean_str(data.get("protocol"), 20),
            port=_coerce_int(data.get("port")),
            description=_clean_str(data.get("description")),
        ))

    for data in requirements_raw:
        key = _clean_str(data.get("key"), 120)
        if not key:
            continue
        requirement_type = (data.get("requirementType") or "env_var").strip()
        if requirement_type not in VALID_REQUIREMENT_TYPES:
            requirement_type = "env_var"
        value_source = (data.get("valueSource") or "manual").strip()
        if value_source not in VALID_VALUE_SOURCES:
            value_source = "manual"
        component_ref = data.get("componentTempId")
        if component_ref is None:
            component_ref = data.get("componentId")
        db.session.add(ServiceBlueprintRequirement(
            blueprint_id=blueprint.id,
            component_id=_resolve(component_ref),
            key=key,
            requirement_type=requirement_type,
            required=_coerce_bool(data.get("required"), True),
            default_value=_clean_str(data.get("defaultValue")),
            allowed_values=_coerce_json(data.get("allowedValues")),
            value_source=value_source,
            secret=_coerce_bool(data.get("secret"), False),
            auto_generate=_coerce_bool(data.get("autoGenerate"), False),
            description=_clean_str(data.get("description")),
        ))


def _validate_top_level(payload: Dict[str, Any], existing_id: Optional[int] = None) -> Tuple[Optional[str], int]:
    name = _clean_str(payload.get("name"), 120)
    if not name:
        return "Blueprint name is required.", 400
    existing = ServiceBlueprint.query.filter_by(name=name).first()
    if existing and existing.id != existing_id:
        return f"A service blueprint named '{name}' already exists.", 409
    status = (payload.get("status") or "draft").strip()
    if status not in VALID_STATUSES:
        return f"Invalid status '{status}'.", 400
    return None, 200


# ---------------------------------------------------------------------------
# Public CRUD API
# ---------------------------------------------------------------------------

def list_blueprints() -> Dict[str, Any]:
    # Drop instances whose App Service mirror was deleted so the per-card
    # "deployed" counts are accurate.
    from .blueprint_deploy_service import prune_orphaned_app_services

    prune_orphaned_app_services()
    blueprints = ServiceBlueprint.query.order_by(ServiceBlueprint.name.asc()).all()
    items = [_blueprint_summary(bp) for bp in blueprints]
    return {"items": items, "count": len(items)}


def get_blueprint(blueprint_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    bp = ServiceBlueprint.query.get(blueprint_id)
    if not bp:
        return None, "Service blueprint not found", 404
    return _blueprint_to_dict(bp), None, 200


def create_blueprint(
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    error, status = _validate_top_level(payload)
    if error:
        return None, error, status

    bp = ServiceBlueprint(
        name=_clean_str(payload.get("name"), 120),
        description=_clean_str(payload.get("description")),
        category=_clean_str(payload.get("category"), 80),
        owner_team=_clean_str(payload.get("ownerTeam"), 255),
        criticality=_clean_str(payload.get("criticality"), 32),
        status=(payload.get("status") or "draft").strip(),
        version=_clean_str(payload.get("version"), 32) or "1.0.0",
        created_by_user_id=actor_user_id,
    )
    db.session.add(bp)
    db.session.flush()
    _save_children(bp, payload)
    db.session.commit()

    log_audit(
        "blueprint_created",
        actor_user_id=actor_user_id,
        target_type="service_blueprint",
        target_id=str(bp.id),
        details={
            "name": bp.name,
            "componentCount": len(bp.components),
            "connectionCount": len(bp.connections),
            "requirementCount": len(bp.requirements),
        },
    )
    data, _, _ = get_blueprint(bp.id)
    return data, None, 201


def update_blueprint(
    blueprint_id: int,
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    bp = ServiceBlueprint.query.get(blueprint_id)
    if not bp:
        return None, "Service blueprint not found", 404

    error, status = _validate_top_level(payload, existing_id=blueprint_id)
    if error:
        return None, error, status

    bp.name = _clean_str(payload.get("name"), 120)
    bp.description = _clean_str(payload.get("description"))
    bp.category = _clean_str(payload.get("category"), 80)
    bp.owner_team = _clean_str(payload.get("ownerTeam"), 255)
    bp.criticality = _clean_str(payload.get("criticality"), 32)
    bp.status = (payload.get("status") or "draft").strip()
    if payload.get("version"):
        bp.version = _clean_str(payload.get("version"), 32) or bp.version
    bp.updated_at = datetime.now(timezone.utc)

    _save_children(bp, payload)
    db.session.commit()

    log_audit(
        "blueprint_updated",
        actor_user_id=actor_user_id,
        target_type="service_blueprint",
        target_id=str(bp.id),
        details={
            "name": bp.name,
            "componentCount": len(bp.components),
            "connectionCount": len(bp.connections),
            "requirementCount": len(bp.requirements),
        },
    )
    data, _, _ = get_blueprint(bp.id)
    return data, None, 200


def delete_blueprint(
    blueprint_id: int,
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    bp = ServiceBlueprint.query.get(blueprint_id)
    if not bp:
        return None, "Service blueprint not found", 404

    from ..models import ApplicationService
    from .blueprint_deploy_service import prune_orphaned_app_services

    # Clear instances whose App Service mirror was already deleted (dangling),
    # so a blueprint isn't blocked by orphaned rows.
    prune_orphaned_app_services()

    # An instance is "live" only if it still has a real ApplicationService mirror.
    # Block deletion when any live instance exists; otherwise remove the remaining
    # mirror-less "ghost" instances (e.g. pre-bridge deploys) along with the blueprint.
    instances = list(bp.app_services)
    live = [
        inst for inst in instances
        if inst.application_service_id and ApplicationService.query.get(inst.application_service_id)
    ]
    if live:
        return None, (
            "Cannot delete a blueprint that still has deployed app services. "
            "Delete its app services first."
        ), 409
    for inst in instances:
        db.session.delete(inst)
    db.session.flush()

    name = bp.name
    db.session.delete(bp)
    db.session.commit()
    log_audit(
        "blueprint_deleted",
        actor_user_id=actor_user_id,
        target_type="service_blueprint",
        target_id=str(blueprint_id),
        details={"name": name},
    )
    return {"id": blueprint_id, "deleted": True}, None, 200
