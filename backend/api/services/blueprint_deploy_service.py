"""Deploy From Blueprint — turn a logical blueprint into a real AppService.

Two phases:

1. ``build_deploy_plan`` produces the *pre-filled* wizard data for a chosen
   target (client/environment/cluster/namespace): generated resource names,
   suggested namespace, kubesight.io labels, per-component recommended mapping,
   smart defaults (ports/resources/health/HPA/template), and the list of
   requirement values the deployer still has to supply.

2. ``deploy_from_blueprint`` persists the resolved choices: it creates an
   :class:`AppService` and one :class:`AppServiceComponentMapping` per logical
   component (create_new / existing_resource / external_dependency / skip), links
   the client, and audits the action. Actual materialization of new Kubernetes
   objects is performed later by the deployment pipeline; mappings created here
   carry the generated names + labels needed to do so.

Runtime topology (``get_app_service``) is resolved from the blueprint + mappings
and the kubesight.io/* labels — never from hardcoded object names.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import (
    AppService,
    AppServiceComponentMapping,
    Client,
    ServiceBlueprint,
    ServiceBlueprintComponent,
)

VALID_ENVIRONMENTS = ("dev", "staging", "production", "custom")
VALID_MAPPING_TYPES = ("create_new", "existing_resource", "external_dependency", "skip")

_ENV_SHORT = {"dev": "dev", "staging": "staging", "production": "prod"}

# Logical component type -> default Kubernetes kind for create-new / runtime view.
_COMPONENT_KIND = {
    "deployment": "Deployment",
    "worker": "Deployment",
    "cache": "Deployment",
    "redis": "StatefulSet",
    "database": "StatefulSet",
    "statefulset": "StatefulSet",
    "kafka": "StatefulSet",
    "queue": "StatefulSet",
    "daemonset": "DaemonSet",
    "cronjob": "CronJob",
    "service": "Service",
    "ingress": "Ingress",
    "external_service": "External",
}

LABEL_APP_SERVICE = "kubesight.io/app-service-id"
LABEL_BLUEPRINT = "kubesight.io/blueprint"
LABEL_COMPONENT = "kubesight.io/component"
LABEL_CLIENT = "kubesight.io/client"
LABEL_ENVIRONMENT = "kubesight.io/environment"


# ---------------------------------------------------------------------------
# Naming / label helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:max_len].strip("-")


def _env_short(environment: Optional[str]) -> str:
    env = (environment or "").strip().lower()
    return _ENV_SHORT.get(env, _slugify(env, 16) or "env")


def _component_slug(component: ServiceBlueprintComponent) -> str:
    return _slugify(component.role or component.name or component.component_type or "component", 30)


def _kind_for(component: ServiceBlueprintComponent) -> str:
    return _COMPONENT_KIND.get(component.component_type, "Deployment")


def generate_app_service_name(blueprint: ServiceBlueprint, environment: Optional[str]) -> str:
    env = (environment or "").strip().title() or "Custom"
    return f"{blueprint.name} - {env}"


def generate_resource_name(
    blueprint: ServiceBlueprint,
    component: ServiceBlueprintComponent,
    environment: Optional[str],
) -> str:
    parts = [_slugify(blueprint.name, 30), _component_slug(component), _env_short(environment)]
    return "-".join(p for p in parts if p)[:253]


def suggest_namespace(client: Optional[Client], environment: Optional[str]) -> str:
    base = _slugify(client.name, 30) if client else "app"
    return f"{base}-{_env_short(environment)}"


def build_labels(
    app_service_slug: str,
    blueprint: ServiceBlueprint,
    component: Optional[ServiceBlueprintComponent],
    client: Optional[Client],
    environment: Optional[str],
) -> Dict[str, str]:
    labels = {
        LABEL_APP_SERVICE: app_service_slug,
        LABEL_BLUEPRINT: _slugify(blueprint.name, 60),
        LABEL_ENVIRONMENT: (environment or "").strip().lower() or "custom",
    }
    if component is not None:
        labels[LABEL_COMPONENT] = _component_slug(component)
    if client is not None:
        labels[LABEL_CLIENT] = _slugify(client.name, 60)
    return labels


def _recommended_mapping_type(component: ServiceBlueprintComponent) -> str:
    if component.component_type == "external_service" and component.supports_external:
        return "external_dependency"
    return "create_new"


def _component_options(component: ServiceBlueprintComponent) -> List[str]:
    options = ["create_new", "existing_resource"]
    if component.supports_external or component.component_type in ("database", "external_service"):
        options.append("external_dependency")
    if not component.required:
        options.append("skip")
    return options


def _default_config(component: ServiceBlueprintComponent) -> Dict[str, Any]:
    return {
        "templateId": component.default_template_id,
        "port": component.default_port,
        "resources": component.default_resources,
        "health": component.default_health,
        "hpa": component.default_hpa,
        "values": component.default_values,
    }


# ---------------------------------------------------------------------------
# Build deploy plan (wizard pre-fill)
# ---------------------------------------------------------------------------

def build_deploy_plan(
    blueprint_id: int,
    target: Dict[str, Any],
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    bp = ServiceBlueprint.query.get(blueprint_id)
    if not bp:
        return None, "Service blueprint not found", 404

    environment = (target.get("environment") or "").strip().lower() or None
    cluster_id = (target.get("clusterId") or "").strip() or None
    client = None
    client_id = target.get("clientId")
    if client_id:
        client = Client.query.get(int(client_id)) if str(client_id).isdigit() else None

    namespace = (target.get("namespace") or "").strip() or suggest_namespace(client, environment)
    app_service_name = (target.get("name") or "").strip() or generate_app_service_name(bp, environment)
    app_service_slug = _slugify(app_service_name, 180)

    components = sorted(bp.components, key=lambda c: (c.position, c.id))
    component_plans: List[Dict[str, Any]] = []
    for component in components:
        recommended = _recommended_mapping_type(component)
        generated_name = generate_resource_name(bp, component, environment)
        component_plans.append({
            "componentId": component.id,
            "name": component.name,
            "role": component.role or "",
            "componentType": component.component_type,
            "kind": _kind_for(component),
            "required": bool(component.required),
            "supportsExternal": bool(component.supports_external),
            "optional": not component.required,
            "recommendedMappingType": recommended,
            "options": _component_options(component),
            "generatedName": generated_name,
            "defaultTemplateId": component.default_template_id,
            "defaultConfig": _default_config(component),
            "labels": build_labels(app_service_slug, bp, component, client, environment),
        })

    # Requirement values the deployer must still provide: required, manual entry,
    # no default and not auto-generated.
    missing_values: List[Dict[str, Any]] = []
    for req in bp.requirements:
        auto_filled = (
            bool(req.auto_generate)
            or (req.default_value not in (None, ""))
            or req.value_source in ("generated", "blueprint_default")
        )
        if req.required and not auto_filled:
            missing_values.append({
                "requirementId": req.id,
                "componentId": req.component_id,
                "key": req.key,
                "requirementType": req.requirement_type,
                "valueSource": req.value_source,
                "secret": bool(req.secret),
                "allowedValues": req.allowed_values,
                "description": req.description or "",
            })

    plan = {
        "blueprintId": bp.id,
        "blueprintName": bp.name,
        "appServiceName": app_service_name,
        "appServiceSlug": app_service_slug,
        "namespace": namespace,
        "namespaceSuggested": not (target.get("namespace") or "").strip(),
        "environment": environment,
        "clusterId": cluster_id,
        "clientId": client.id if client else None,
        "clientName": client.name if client else None,
        "baseLabels": build_labels(app_service_slug, bp, None, client, environment),
        "components": component_plans,
        "missingValues": missing_values,
    }
    return plan, None, 200


# ---------------------------------------------------------------------------
# Serializers for AppService + runtime topology
# ---------------------------------------------------------------------------

def _mapping_to_dict(m: AppServiceComponentMapping) -> Dict[str, Any]:
    resolved = m.external_endpoint if m.mapping_type == "external_dependency" else (
        f"{(m.kubernetes_kind or '').lower()}/{m.kubernetes_name or m.generated_name or ''}".strip("/")
    )
    return {
        "id": m.id,
        "componentId": m.blueprint_component_id,
        "componentName": m.component_name or "",
        "componentRole": m.component_role or "",
        "mappingType": m.mapping_type,
        "kind": m.kubernetes_kind,
        "name": m.kubernetes_name,
        "generatedName": m.generated_name,
        "namespace": m.namespace,
        "clusterId": m.cluster_id,
        "externalEndpoint": m.external_endpoint,
        "status": m.status,
        "resolved": resolved,
        "labels": m.labels,
        "config": m.config,
    }


def _runtime_topology(app_service: AppService) -> Dict[str, Any]:
    """Resolve the logical blueprint topology to the mapped runtime resources.

    Nodes are the logical components (one node each, even when a component maps to
    multiple pods/resources); edges come from the blueprint's logical connections
    and are dropped only when one side was skipped.
    """
    mappings = list(app_service.component_mappings)
    by_component = {m.blueprint_component_id: m for m in mappings if m.blueprint_component_id}

    nodes = []
    for m in sorted(mappings, key=lambda x: x.id):
        node = _mapping_to_dict(m)
        node["nodeId"] = m.blueprint_component_id or f"m{m.id}"
        nodes.append(node)

    edges = []
    bp = app_service.blueprint
    if bp is not None:
        for conn in bp.connections:
            src = by_component.get(conn.source_component_id)
            tgt = by_component.get(conn.target_component_id)
            if not src or not tgt:
                continue
            if src.mapping_type == "skip" or tgt.mapping_type == "skip":
                continue
            edges.append({
                "sourceNodeId": conn.source_component_id,
                "targetNodeId": conn.target_component_id,
                "protocol": conn.protocol,
                "port": conn.port,
                "connectionType": conn.connection_type,
                "description": conn.description or "",
            })
    return {"nodes": nodes, "edges": edges}


def _app_service_to_dict(app_service: AppService, *, detailed: bool = False) -> Dict[str, Any]:
    bp = app_service.blueprint
    client = app_service.client
    data = {
        "id": app_service.id,
        "name": app_service.name,
        "slug": app_service.slug,
        "description": app_service.description or "",
        "blueprintId": app_service.blueprint_id,
        "blueprintName": bp.name if bp else None,
        "clientId": app_service.client_id,
        "clientName": client.name if client else None,
        "applicationServiceId": app_service.application_service_id,
        "environment": app_service.environment,
        "clusterId": app_service.cluster_id,
        "namespace": app_service.namespace,
        "status": app_service.status,
        "componentCount": len(app_service.component_mappings),
        "createdAt": app_service.created_at.isoformat() if app_service.created_at else None,
        "updatedAt": app_service.updated_at.isoformat() if app_service.updated_at else None,
    }
    if detailed:
        data["mappings"] = [_mapping_to_dict(m) for m in sorted(app_service.component_mappings, key=lambda x: x.id)]
        data["topology"] = _runtime_topology(app_service)
    return data


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

def deploy_from_blueprint(
    blueprint_id: int,
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    bp = ServiceBlueprint.query.get(blueprint_id)
    if not bp:
        return None, "Service blueprint not found", 404

    environment = (payload.get("environment") or "").strip().lower() or None
    cluster_id = (payload.get("clusterId") or "").strip() or None
    namespace = (payload.get("namespace") or "").strip() or None

    client = None
    client_id = payload.get("clientId")
    if client_id and str(client_id).isdigit():
        client = Client.query.get(int(client_id))
        if not client:
            return None, "Client not found", 404

    if not namespace:
        namespace = suggest_namespace(client, environment)

    name = (payload.get("name") or "").strip() or generate_app_service_name(bp, environment)
    if AppService.query.filter_by(name=name).first():
        return None, f"An app service named '{name}' already exists.", 409
    app_service_slug = _slugify(name, 180)

    app_service = AppService(
        name=name,
        slug=app_service_slug,
        description=_clean(payload.get("description")),
        client_id=client.id if client else None,
        blueprint_id=bp.id,
        environment=environment,
        cluster_id=cluster_id,
        namespace=namespace,
        status="planned",
        created_by_user_id=actor_user_id,
    )
    db.session.add(app_service)
    db.session.flush()

    components_by_id = {c.id: c for c in bp.components}
    mappings_payload = payload.get("mappings") or []

    # Index incoming mappings by componentId so we can fall back to a sensible
    # default for any component the caller omitted.
    incoming = {}
    for raw in mappings_payload:
        cid = raw.get("componentId")
        if cid is not None and str(cid).isdigit():
            incoming[int(cid)] = raw

    created = 0
    for component in sorted(bp.components, key=lambda c: (c.position, c.id)):
        raw = incoming.get(component.id, {})
        mapping_type = (raw.get("mappingType") or _recommended_mapping_type(component)).strip()
        if mapping_type not in VALID_MAPPING_TYPES:
            mapping_type = "create_new"
        if mapping_type == "skip" and component.required:
            mapping_type = "create_new"  # required components cannot be skipped

        generated_name = _clean(raw.get("generatedName")) or generate_resource_name(bp, component, environment)
        kind = _clean(raw.get("kind")) or _kind_for(component)
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else build_labels(
            app_service_slug, bp, component, client, environment
        )

        if mapping_type == "create_new":
            k8s_name, status = generated_name, "planned"
            external_endpoint = None
        elif mapping_type == "existing_resource":
            k8s_name = _clean(raw.get("name"))
            status = "linked"
            external_endpoint = None
        elif mapping_type == "external_dependency":
            k8s_name = None
            kind = "External"
            external_endpoint = _clean(raw.get("externalEndpoint"))
            status = "linked"
        else:  # skip
            k8s_name, status, external_endpoint = None, "skipped", None

        db.session.add(AppServiceComponentMapping(
            app_service_id=app_service.id,
            blueprint_component_id=component.id,
            component_name=component.name,
            component_role=component.role,
            mapping_type=mapping_type,
            kubernetes_kind=kind if mapping_type != "skip" else None,
            kubernetes_name=k8s_name,
            namespace=_clean(raw.get("namespace")) or namespace,
            cluster_id=_clean(raw.get("clusterId")) or cluster_id,
            external_endpoint=external_endpoint,
            status=status,
            generated_name=generated_name if mapping_type == "create_new" else None,
            labels=labels,
            config=raw.get("config") if isinstance(raw.get("config"), dict) else _default_config(component),
        ))
        created += 1

    db.session.commit()

    # Bridge into the operational App Services tab so the instance is visible
    # there with workloads/health/topology and is linked to the client.
    _link_application_service(app_service, bp, client, actor_user_id)

    log_audit(
        "app_service_created_from_blueprint",
        actor_user_id=actor_user_id,
        target_type="app_service",
        target_id=str(app_service.id),
        details={
            "name": name,
            "blueprintId": bp.id,
            "blueprintName": bp.name,
            "clientId": client.id if client else None,
            "environment": environment,
            "clusterId": cluster_id,
            "namespace": namespace,
            "componentMappings": created,
        },
    )

    data = _app_service_to_dict(app_service, detailed=True)
    return data, None, 201


# Workload kinds that map to an ApplicationServiceDeployment row (health/replicas).
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob"}


def _link_application_service(
    app_service: AppService,
    bp: ServiceBlueprint,
    client: Optional[Client],
    actor_user_id: Optional[int],
) -> None:
    """Create an ApplicationService mirror of this deploy so it appears in the
    App Services tab (workloads, health, runtime topology) and is linked to the
    client. Best-effort: never fails the deploy. Idempotent enough — only runs
    once per deploy and skips if already linked.
    """
    from ..models import ApplicationService, ClientApplicationService
    from .application_service_service import create_service

    if app_service.application_service_id:
        return

    mappings = sorted(app_service.component_mappings, key=lambda m: m.id)

    deployments = []
    nodes = []
    for m in mappings:
        temp_id = str(m.blueprint_component_id or f"m{m.id}")
        resolved_name = m.kubernetes_name or m.generated_name
        linked = (
            m.mapping_type in ("create_new", "existing_resource")
            and (m.kubernetes_kind in _WORKLOAD_KINDS)
            and resolved_name
        )
        if linked:
            deployments.append({
                "clusterId": m.cluster_id or app_service.cluster_id,
                "namespace": m.namespace or app_service.namespace,
                "deploymentName": resolved_name,
                "kind": "deployment",
            })
        node_desc = None
        if m.mapping_type == "external_dependency":
            node_desc = f"External: {m.external_endpoint or ''}".strip()
        elif m.mapping_type == "skip":
            node_desc = "Skipped"
        nodes.append({
            "tempId": temp_id,
            "name": m.component_name or temp_id,
            "type": m.component_role or (m.kubernetes_kind or "component"),
            "description": node_desc,
            "linkedClusterId": (m.cluster_id or app_service.cluster_id) if linked else None,
            "linkedNamespace": (m.namespace or app_service.namespace) if linked else None,
            "linkedDeployment": resolved_name if linked else None,
        })

    edges = [
        {
            "sourceTempId": str(conn.source_component_id),
            "targetTempId": str(conn.target_component_id),
            "protocol": conn.protocol,
            "scope": "internal",
            "description": conn.description,
        }
        for conn in bp.connections
    ]

    description = f"Deployed from blueprint '{bp.name}'"
    if app_service.environment:
        description += f" ({app_service.environment})"

    base_name = app_service.name
    payload = {
        "name": base_name,
        "description": description,
        "deployments": deployments,
        "topology": {"nodes": nodes, "edges": edges},
    }

    data, error, status = create_service(payload, actor_user_id=actor_user_id)
    if error and status == 409:
        # Name already used by a manual App Service — disambiguate and retry once.
        payload["name"] = f"{base_name} ({app_service.id})"
        data, error, status = create_service(payload, actor_user_id=actor_user_id)
    if error or not data:
        return

    service_id = data["id"]
    if client is not None:
        if not ClientApplicationService.query.filter_by(
            client_id=client.id, service_id=service_id
        ).first():
            db.session.add(ClientApplicationService(client_id=client.id, service_id=service_id))
    app_service.application_service_id = service_id
    app_service.status = "active"
    db.session.commit()


# ---------------------------------------------------------------------------
# AppService read API
# ---------------------------------------------------------------------------

def prune_orphaned_app_services() -> int:
    """Delete blueprint instances whose ApplicationService mirror was removed
    (e.g. deleted from the App Services tab). Keeps the Service Catalog's
    "deployed" counts accurate. Returns the number pruned.
    """
    from ..models import ApplicationService

    removed = 0
    candidates = AppService.query.filter(AppService.application_service_id.isnot(None)).all()
    for instance in candidates:
        if not ApplicationService.query.get(instance.application_service_id):
            db.session.delete(instance)
            removed += 1
    if removed:
        db.session.commit()
    return removed


def list_app_services(
    client_id: Optional[int] = None,
    blueprint_id: Optional[int] = None,
) -> Dict[str, Any]:
    prune_orphaned_app_services()
    query = AppService.query
    if client_id is not None:
        query = query.filter_by(client_id=client_id)
    if blueprint_id is not None:
        query = query.filter_by(blueprint_id=blueprint_id)
    services = query.order_by(AppService.name.asc()).all()
    items = [_app_service_to_dict(s) for s in services]
    return {"items": items, "count": len(items)}


def get_app_service(app_service_id: int, user=None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    app_service = AppService.query.get(app_service_id)
    if not app_service:
        return None, "App service not found", 404
    return _app_service_to_dict(app_service, detailed=True), None, 200


def delete_app_service(
    app_service_id: int,
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    app_service = AppService.query.get(app_service_id)
    if not app_service:
        return None, "App service not found", 404
    name = app_service.name
    # Also remove the operational ApplicationService mirror so it disappears from
    # the App Services tab.
    mirror_id = app_service.application_service_id
    db.session.delete(app_service)
    if mirror_id:
        from ..models import ApplicationService

        mirror = ApplicationService.query.get(mirror_id)
        if mirror:
            db.session.delete(mirror)
    db.session.commit()
    log_audit(
        "app_service_deleted",
        actor_user_id=actor_user_id,
        target_type="app_service",
        target_id=str(app_service_id),
        details={"name": name},
    )
    return {"id": app_service_id, "deleted": True}, None, 200


def _clean(value: Any) -> Optional[str]:
    text = (str(value).strip() if value is not None else "")
    return text or None
