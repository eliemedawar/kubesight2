from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import ApplicationService, ApplicationServiceDeployment, ApplicationServiceTopologyNode, ApplicationServiceTopologyEdge

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

_MOCK_DEPLOYMENT_HEALTH: Dict[Tuple[str, str, str], Dict[str, Any]] = {
    ("cluster-prod", "production", "auth-api"):      {"desired": 3, "available": 3, "ready": 3},
    ("cluster-prod", "production", "auth-worker"):   {"desired": 2, "available": 2, "ready": 2},
    ("cluster-staging", "staging", "auth-api"):      {"desired": 1, "available": 1, "ready": 1},
    ("cluster-prod", "production", "billing-api"):   {"desired": 3, "available": 3, "ready": 3},
    ("cluster-prod", "production", "billing-worker"):{"desired": 2, "available": 1, "ready": 1},
    ("cluster-staging", "staging", "billing-api"):   {"desired": 1, "available": 1, "ready": 1},
    ("cluster-prod", "production", "api-gateway"):   {"desired": 3, "available": 3, "ready": 3},
    ("cluster-prod", "production", "rate-limiter"):  {"desired": 2, "available": 0, "ready": 0},
    ("cluster-prod", "monitoring", "prometheus"):    {"desired": 1, "available": 1, "ready": 1},
    ("cluster-prod", "monitoring", "grafana"):       {"desired": 1, "available": 1, "ready": 1},
    ("cluster-staging", "staging", "user-api"):      {"desired": 2, "available": 2, "ready": 2},
}

def _mk_node(id, name, type=None, desc=None):
    return {"id": id, "name": name, "type": type, "description": desc,
            "linkedClusterId": None, "linkedNamespace": None, "linkedDeployment": None}

def _mk_edge(id, src, tgt):
    return {"id": id, "sourceNodeId": src, "targetNodeId": tgt}


_MOCK_SERVICES = [
    {
        "id": 1,
        "name": "Authorization Service",
        "description": "Handles authentication and authorization across all platform services.",
        "createdAt": "2024-01-15T09:00:00+00:00",
        "updatedAt": "2024-05-20T14:30:00+00:00",
        "deployments": [
            {"id": 1, "serviceId": 1, "clusterId": "cluster-prod",    "namespace": "production", "deploymentName": "auth-api"},
            {"id": 2, "serviceId": 1, "clusterId": "cluster-prod",    "namespace": "production", "deploymentName": "auth-worker"},
            {"id": 3, "serviceId": 1, "clusterId": "cluster-staging", "namespace": "staging",    "deploymentName": "auth-api"},
        ],
        "topology": {
            "nodes": [
                _mk_node(1,  "User",        "Client",          "End user or external caller"),
                _mk_node(2,  "WAF",         "Security",        "Web Application Firewall"),
                _mk_node(3,  "Tyk",         "API Gateway",     "Main ingress gateway"),
                _mk_node(4,  "Verto",       "Microservice",    "Authorization microservice"),
                _mk_node(5,  "Nginx",       "Reverse Proxy",   "Internal reverse proxy"),
                _mk_node(6,  "Auth API",    "Application",     "Core auth logic"),
                _mk_node(7,  "PostgreSQL",  "Database",        "Auth database"),
            ],
            "edges": [
                _mk_edge(1, 1, 2),   # User → WAF
                _mk_edge(2, 2, 3),   # WAF → Tyk
                _mk_edge(3, 3, 4),   # Tyk → Verto
                _mk_edge(4, 3, 5),   # Tyk → Nginx
                _mk_edge(5, 5, 6),   # Nginx → Auth API
                _mk_edge(6, 6, 7),   # Auth API → PostgreSQL
            ],
        },
    },
    {
        "id": 2,
        "name": "Billing Service",
        "description": "Manages subscriptions, invoicing, and payment processing.",
        "createdAt": "2024-02-01T10:00:00+00:00",
        "updatedAt": "2024-06-01T08:00:00+00:00",
        "deployments": [
            {"id": 4, "serviceId": 2, "clusterId": "cluster-prod",    "namespace": "production", "deploymentName": "billing-api"},
            {"id": 5, "serviceId": 2, "clusterId": "cluster-prod",    "namespace": "production", "deploymentName": "billing-worker"},
            {"id": 6, "serviceId": 2, "clusterId": "cluster-staging", "namespace": "staging",    "deploymentName": "billing-api"},
        ],
        "topology": {
            "nodes": [
                _mk_node(8,  "User",        "Client",       None),
                _mk_node(9,  "WAF",         "Security",     None),
                _mk_node(10, "Tyk",         "API Gateway",  None),
                _mk_node(11, "Billing API", "Application",  None),
                _mk_node(12, "Stripe",      "Third Party",  "Payment processor"),
                _mk_node(13, "PostgreSQL",  "Database",     "Primary billing DB"),
                _mk_node(14, "Redis",       "Cache",        "Session and rate-limit cache"),
            ],
            "edges": [
                _mk_edge(7,  8,  9),   # User → WAF
                _mk_edge(8,  9,  10),  # WAF → Tyk
                _mk_edge(9,  10, 11),  # Tyk → Billing API
                _mk_edge(10, 11, 12),  # Billing API → Stripe
                _mk_edge(11, 11, 13),  # Billing API → PostgreSQL
                _mk_edge(12, 11, 14),  # Billing API → Redis
            ],
        },
    },
    {
        "id": 3,
        "name": "API Gateway",
        "description": "Entry point for all external traffic. Handles routing, rate limiting, and SSL termination.",
        "createdAt": "2024-01-10T08:00:00+00:00",
        "updatedAt": "2024-06-05T16:00:00+00:00",
        "deployments": [
            {"id": 7, "serviceId": 3, "clusterId": "cluster-prod", "namespace": "production", "deploymentName": "api-gateway"},
            {"id": 8, "serviceId": 3, "clusterId": "cluster-prod", "namespace": "production", "deploymentName": "rate-limiter"},
        ],
        "topology": {
            "nodes": [
                _mk_node(15, "Client",        "External",     None),
                _mk_node(16, "Cloudflare",    "CDN",          "DDoS protection and CDN"),
                _mk_node(17, "WAF",           "Security",     None),
                _mk_node(18, "Tyk",           "API Gateway",  "Rate limiting and routing"),
                _mk_node(19, "Auth Service",  "Microservice", None),
                _mk_node(20, "User Service",  "Microservice", None),
            ],
            "edges": [
                _mk_edge(13, 15, 16),  # Client → Cloudflare
                _mk_edge(14, 16, 17),  # Cloudflare → WAF
                _mk_edge(15, 17, 18),  # WAF → Tyk
                _mk_edge(16, 18, 19),  # Tyk → Auth Service
                _mk_edge(17, 18, 20),  # Tyk → User Service
            ],
        },
    },
    {
        "id": 4,
        "name": "Monitoring Stack",
        "description": "Centralized metrics collection and visualization.",
        "createdAt": "2024-03-01T11:00:00+00:00",
        "updatedAt": "2024-03-15T09:00:00+00:00",
        "deployments": [
            {"id": 9,  "serviceId": 4, "clusterId": "cluster-prod", "namespace": "monitoring", "deploymentName": "prometheus"},
            {"id": 10, "serviceId": 4, "clusterId": "cluster-prod", "namespace": "monitoring", "deploymentName": "grafana"},
        ],
        "topology": {
            "nodes": [
                _mk_node(21, "Prometheus",     "Metrics",    "Scrapes metrics from all services"),
                _mk_node(22, "Alertmanager",   "Alerting",   "Evaluates alert rules"),
                _mk_node(23, "Email",          "Receiver",   None),
                _mk_node(24, "Slack",          "Receiver",   None),
                _mk_node(25, "PagerDuty",      "Receiver",   "On-call escalation"),
                _mk_node(26, "Grafana",        "Dashboard",  "Metric visualization"),
            ],
            "edges": [
                _mk_edge(18, 21, 22),  # Prometheus → Alertmanager
                _mk_edge(19, 22, 23),  # Alertmanager → Email
                _mk_edge(20, 22, 24),  # Alertmanager → Slack
                _mk_edge(21, 22, 25),  # Alertmanager → PagerDuty
                _mk_edge(22, 21, 26),  # Prometheus → Grafana
            ],
        },
    },
    {
        "id": 5,
        "name": "User Service",
        "description": "User profile management and preferences API.",
        "createdAt": "2024-04-01T12:00:00+00:00",
        "updatedAt": "2024-06-10T10:00:00+00:00",
        "deployments": [
            {"id": 11, "serviceId": 5, "clusterId": "cluster-staging", "namespace": "staging", "deploymentName": "user-api"},
        ],
        "topology": {"nodes": [], "edges": []},
    },
]


# ---------------------------------------------------------------------------
# Health helpers
# ---------------------------------------------------------------------------

def _deployment_status(desired: int, available: int) -> str:
    if desired == 0:
        return "healthy"
    if available == 0:
        return "critical"
    if available < desired:
        return "warning"
    return "healthy"


def _aggregate_health(statuses: List[str]) -> str:
    if not statuses:
        return "unknown"
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "warning"
    return "healthy"


def _mock_deployment_detail(dep: Dict[str, Any]) -> Dict[str, Any]:
    key = (dep["clusterId"], dep["namespace"], dep["deploymentName"])
    health = _MOCK_DEPLOYMENT_HEALTH.get(key, {"desired": 1, "available": 1, "ready": 1})
    desired = health["desired"]
    available = health["available"]
    return {
        "id": dep["id"],
        "serviceId": dep["serviceId"],
        "clusterId": dep["clusterId"],
        "namespace": dep["namespace"],
        "deploymentName": dep["deploymentName"],
        "desiredReplicas": desired,
        "availableReplicas": available,
        "readyReplicas": health["ready"],
        "status": _deployment_status(desired, available),
    }


def _mock_service_health(service: Dict[str, Any]) -> str:
    statuses = [
        _mock_deployment_detail(d)["status"]
        for d in service["deployments"]
    ]
    return _aggregate_health(statuses)


# ---------------------------------------------------------------------------
# K8s live health (real mode)
# ---------------------------------------------------------------------------

def _fetch_namespace_deployment_map(
    cluster_id: str,
    namespace: str,
    user=None,
) -> Dict[str, Dict[str, Any]]:
    """Return {deployment_name: raw_k8s_item} for all deployments in a namespace."""
    from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s
    from ..access_engine import can_access_namespace

    if not should_use_real_k8s(cluster_id):
        return {}
    if user and not can_access_namespace(user, cluster_id, namespace):
        return {}
    access = resolve_cluster_access(cluster_id)
    if not access:
        return {}
    try:
        output = _run_for_access(access, ["get", "deployments", "-n", namespace, "-o", "json"])
        items = json.loads(output).get("items", [])
        return {
            item.get("metadata", {}).get("name", ""): item
            for item in items
            if item.get("metadata", {}).get("name")
        }
    except (K8sCommandError, Exception):
        return {}


def _live_deployment_detail(dep_row, k8s_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    item = k8s_map.get(dep_row.deployment_name)
    if item:
        spec = item.get("spec", {})
        status = item.get("status", {})
        desired = spec.get("replicas") or 0
        available = status.get("availableReplicas") or 0
        ready = status.get("readyReplicas") or 0
    else:
        desired, available, ready = 0, 0, 0  # cluster unreachable or deployment not found → unknown

    return {
        "id": dep_row.id,
        "serviceId": dep_row.service_id,
        "clusterId": dep_row.cluster_id,
        "namespace": dep_row.namespace,
        "deploymentName": dep_row.deployment_name,
        "kind": getattr(dep_row, "resource_kind", None) or "deployment",
        "desiredReplicas": desired,
        "availableReplicas": available,
        "readyReplicas": ready,
        "status": _deployment_status(desired, available),
    }


def _build_k8s_health_map(
    deployments: List,
    user=None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Fetch all unique (cluster, namespace) pairs in parallel and return a map."""
    pairs = list({(d.cluster_id, d.namespace) for d in deployments})
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def fetch(cluster_id: str, namespace: str):
        return (cluster_id, namespace), _fetch_namespace_deployment_map(cluster_id, namespace, user=user)

    with ThreadPoolExecutor(max_workers=min(len(pairs), 8)) as pool:
        futures = {pool.submit(fetch, c, n): (c, n) for c, n in pairs}
        for fut in as_completed(futures):
            try:
                key, data = fut.result()
                result[key] = data
            except Exception:
                pass
    return result


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _dep_to_dict(dep: ApplicationServiceDeployment) -> Dict[str, Any]:
    return {
        "id": dep.id,
        "serviceId": dep.service_id,
        "clusterId": dep.cluster_id,
        "namespace": dep.namespace,
        "deploymentName": dep.deployment_name,
        "kind": getattr(dep, "resource_kind", None) or "deployment",
        "createdAt": dep.created_at.isoformat() if dep.created_at else None,
    }


def _topology_to_dict(svc: ApplicationService) -> Dict[str, Any]:
    nodes = sorted(svc.topology_nodes, key=lambda n: n.id)
    edges = sorted(svc.topology_edges, key=lambda e: e.id)
    return {
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.type,
                "description": n.description,
                "linkedClusterId": n.linked_cluster_id,
                "linkedNamespace": n.linked_namespace,
                "linkedDeployment": n.linked_deployment,
            }
            for n in nodes
        ],
        "edges": [
            {
                "id": e.id,
                "sourceNodeId": e.source_node_id,
                "targetNodeId": e.target_node_id,
            }
            for e in edges
        ],
    }


def _service_to_dict(
    svc: ApplicationService,
    deployment_details: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    deps = deployment_details if deployment_details is not None else [_dep_to_dict(d) for d in svc.deployments]
    statuses = [d.get("status", "unknown") for d in deps if "status" in d]
    health = _aggregate_health(statuses) if statuses else "unknown"
    return {
        "id": svc.id,
        "name": svc.name,
        "description": svc.description or "",
        "deploymentCount": len(svc.deployments),
        "deployments": deps,
        "health": health,
        "topology": _topology_to_dict(svc),
        "createdAt": svc.created_at.isoformat() if svc.created_at else None,
        "updatedAt": svc.updated_at.isoformat() if svc.updated_at else None,
    }


def _service_list_item(
    svc: ApplicationService,
    k8s_health_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    user=None,
) -> Dict[str, Any]:
    if k8s_health_map is not None:
        deps = [
            _live_deployment_detail(d, k8s_health_map.get((d.cluster_id, d.namespace), {}))
            for d in svc.deployments
        ]
    else:
        deps = [_dep_to_dict(d) for d in svc.deployments]
    return _service_to_dict(svc, deployment_details=deps)


# ---------------------------------------------------------------------------
# Topology helper
# ---------------------------------------------------------------------------

def _save_topology(service_id: int, topology_payload: Dict[str, Any]) -> None:
    """Replace all topology nodes and edges for service_id."""
    # Edges must be deleted before nodes due to FK constraints.
    ApplicationServiceTopologyEdge.query.filter_by(service_id=service_id).delete()
    ApplicationServiceTopologyNode.query.filter_by(service_id=service_id).delete()
    db.session.flush()

    nodes_raw = topology_payload.get("nodes") or []
    edges_raw = topology_payload.get("edges") or []

    temp_to_id: Dict[str, int] = {}
    for node_data in nodes_raw:
        name = (node_data.get("name") or "").strip()
        if not name:
            continue
        node = ApplicationServiceTopologyNode(
            service_id=service_id,
            name=name,
            type=(node_data.get("type") or "").strip() or None,
            description=(node_data.get("description") or "").strip() or None,
            linked_cluster_id=(node_data.get("linkedClusterId") or "").strip() or None,
            linked_namespace=(node_data.get("linkedNamespace") or "").strip() or None,
            linked_deployment=(node_data.get("linkedDeployment") or "").strip() or None,
        )
        db.session.add(node)
        db.session.flush()
        temp_id = str(node_data.get("tempId") or "")
        if temp_id:
            temp_to_id[temp_id] = node.id

    seen_edges: set = set()
    for edge_data in edges_raw:
        src_temp = str(edge_data.get("sourceTempId") or "")
        tgt_temp = str(edge_data.get("targetTempId") or "")
        src_id = temp_to_id.get(src_temp)
        tgt_id = temp_to_id.get(tgt_temp)
        if not src_id or not tgt_id or src_id == tgt_id:
            continue
        key = (src_id, tgt_id)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        db.session.add(ApplicationServiceTopologyEdge(
            service_id=service_id,
            source_node_id=src_id,
            target_node_id=tgt_id,
        ))


# ---------------------------------------------------------------------------
# Public CRUD API
# ---------------------------------------------------------------------------

def list_services(user=None) -> Dict[str, Any]:
    from ..k8s_provider import should_use_real_k8s
    services = ApplicationService.query.order_by(ApplicationService.name.asc()).all()

    # Collect all deployment rows and fetch live health if in real mode.
    all_deployments = [d for svc in services for d in svc.deployments]
    k8s_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None
    if all_deployments and any(should_use_real_k8s(d.cluster_id) for d in all_deployments):
        k8s_map = _build_k8s_health_map(all_deployments, user=user)

    items = [_service_list_item(svc, k8s_health_map=k8s_map, user=user) for svc in services]
    return {"items": items, "count": len(items)}


def list_services_mock() -> Dict[str, Any]:
    items = []
    for svc in _MOCK_SERVICES:
        dep_details = [_mock_deployment_detail(d) for d in svc["deployments"]]
        statuses = [d["status"] for d in dep_details]
        items.append({
            "id": svc["id"],
            "name": svc["name"],
            "description": svc["description"],
            "deploymentCount": len(svc["deployments"]),
            "deployments": dep_details,
            "health": _aggregate_health(statuses),
            "topology": svc.get("topology", {"nodes": [], "edges": []}),
            "createdAt": svc["createdAt"],
            "updatedAt": svc["updatedAt"],
        })
    return {"items": items, "count": len(items)}


def get_service(service_id: int, user=None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    svc = ApplicationService.query.get(service_id)
    if not svc:
        return None, "Application service not found", 404

    from ..k8s_provider import should_use_real_k8s
    if svc.deployments and any(should_use_real_k8s(d.cluster_id) for d in svc.deployments):
        k8s_map = _build_k8s_health_map(list(svc.deployments), user=user)
        deps = [
            _live_deployment_detail(d, k8s_map.get((d.cluster_id, d.namespace), {}))
            for d in svc.deployments
        ]
    else:
        deps = [_dep_to_dict(d) for d in svc.deployments]

    return _service_to_dict(svc, deployment_details=deps), None, 200


def get_service_mock(service_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    svc = next((s for s in _MOCK_SERVICES if s["id"] == service_id), None)
    if not svc:
        return None, "Application service not found", 404
    dep_details = [_mock_deployment_detail(d) for d in svc["deployments"]]
    statuses = [d["status"] for d in dep_details]
    data = {
        "id": svc["id"],
        "name": svc["name"],
        "description": svc["description"],
        "deploymentCount": len(svc["deployments"]),
        "deployments": dep_details,
        "health": _aggregate_health(statuses),
        "topology": svc.get("topology", {"nodes": [], "edges": []}),
        "createdAt": svc["createdAt"],
        "updatedAt": svc["updatedAt"],
    }
    return data, None, 200


def create_service(
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Service name is required.", 400
    if len(name) > 120:
        return None, "Service name must be 120 characters or less.", 400

    if ApplicationService.query.filter_by(name=name).first():
        return None, f"An application service named '{name}' already exists.", 409

    description = (payload.get("description") or "").strip() or None
    deployments_payload = payload.get("deployments") or []

    svc = ApplicationService(name=name, description=description)
    db.session.add(svc)
    db.session.flush()

    seen: set = set()
    for dep in deployments_payload:
        cluster_id = (dep.get("clusterId") or "").strip()
        namespace = (dep.get("namespace") or "").strip()
        deployment_name = (dep.get("deploymentName") or "").strip()
        resource_kind = dep.get("kind", "deployment") or "deployment"
        if resource_kind not in ("deployment", "pod"):
            resource_kind = "deployment"
        if not (cluster_id and namespace and deployment_name):
            continue
        key = (cluster_id, namespace, deployment_name, resource_kind)
        if key in seen:
            continue
        seen.add(key)
        db.session.add(ApplicationServiceDeployment(
            service_id=svc.id,
            cluster_id=cluster_id,
            namespace=namespace,
            deployment_name=deployment_name,
            resource_kind=resource_kind,
        ))

    topology_payload = payload.get("topology") or {}
    _save_topology(svc.id, topology_payload)

    db.session.commit()
    log_audit(
        "app_service_created",
        actor_user_id=actor_user_id,
        target_type="application_service",
        target_id=str(svc.id),
        details={"name": name, "deploymentCount": len(seen), "topologyNodeCount": len(topology_payload.get("nodes") or [])},
    )
    data, _, _ = get_service(svc.id)
    return data, None, 201


def update_service(
    service_id: int,
    payload: Dict[str, Any],
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    svc = ApplicationService.query.get(service_id)
    if not svc:
        return None, "Application service not found", 404

    name = (payload.get("name") or "").strip()
    if not name:
        return None, "Service name is required.", 400
    if len(name) > 120:
        return None, "Service name must be 120 characters or less.", 400

    existing = ApplicationService.query.filter_by(name=name).first()
    if existing and existing.id != service_id:
        return None, f"An application service named '{name}' already exists.", 409

    svc.name = name
    svc.description = (payload.get("description") or "").strip() or None
    svc.updated_at = datetime.now(timezone.utc)

    # Replace deployments entirely.
    for dep in list(svc.deployments):
        db.session.delete(dep)
    db.session.flush()

    deployments_payload = payload.get("deployments") or []
    seen: set = set()
    for dep in deployments_payload:
        cluster_id = (dep.get("clusterId") or "").strip()
        namespace = (dep.get("namespace") or "").strip()
        deployment_name = (dep.get("deploymentName") or "").strip()
        resource_kind = dep.get("kind", "deployment") or "deployment"
        if resource_kind not in ("deployment", "pod"):
            resource_kind = "deployment"
        if not (cluster_id and namespace and deployment_name):
            continue
        key = (cluster_id, namespace, deployment_name, resource_kind)
        if key in seen:
            continue
        seen.add(key)
        db.session.add(ApplicationServiceDeployment(
            service_id=svc.id,
            cluster_id=cluster_id,
            namespace=namespace,
            deployment_name=deployment_name,
            resource_kind=resource_kind,
        ))

    topology_payload = payload.get("topology") or {}
    _save_topology(svc.id, topology_payload)

    db.session.commit()
    log_audit(
        "app_service_updated",
        actor_user_id=actor_user_id,
        target_type="application_service",
        target_id=str(svc.id),
        details={"name": name, "deploymentCount": len(seen), "topologyNodeCount": len(topology_payload.get("nodes") or [])},
    )
    data, _, _ = get_service(svc.id)
    return data, None, 200


def delete_service(
    service_id: int,
    actor_user_id: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    svc = ApplicationService.query.get(service_id)
    if not svc:
        return None, "Application service not found", 404

    name = svc.name
    db.session.delete(svc)
    db.session.commit()
    log_audit(
        "app_service_deleted",
        actor_user_id=actor_user_id,
        target_type="application_service",
        target_id=str(service_id),
        details={"name": name},
    )
    return {"id": service_id, "deleted": True}, None, 200


# ---------------------------------------------------------------------------
# Deployment picker — list deployments available in a namespace (respecting RBAC)
# ---------------------------------------------------------------------------

def list_picker_deployments(
    cluster_id: str,
    namespace: str,
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s
    from ..access_engine import can_access_cluster, can_access_namespace

    if not cluster_id or not namespace:
        return None, "clusterId and namespace are required.", 400

    if user:
        from ..access_engine import can_access_cluster as cac, can_access_namespace as can
        if not cac(user, cluster_id):
            return None, "Forbidden", 403
        if not can(user, cluster_id, namespace):
            return None, "Forbidden", 403

    if not should_use_real_k8s(cluster_id):
        # Return mock deployment names for this cluster/namespace
        names = list({
            dep_name
            for (cid, ns, dep_name) in _MOCK_DEPLOYMENT_HEALTH
            if cid == cluster_id and ns == namespace
        })
        return {"items": sorted(names), "count": len(names)}, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        output = _run_for_access(access, ["get", "deployments", "-n", namespace, "-o", "json"])
        items = json.loads(output).get("items", [])
        names = sorted(
            item.get("metadata", {}).get("name", "")
            for item in items
            if item.get("metadata", {}).get("name")
        )
        return {"items": names, "count": len(names)}, None, 200
    except K8sCommandError as exc:
        return None, f"Failed to list deployments: {exc}", 503


# ---------------------------------------------------------------------------
# Pod picker — list pods available in a namespace (respecting RBAC)
# ---------------------------------------------------------------------------

def list_picker_pods(
    cluster_id: str,
    namespace: str,
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s
    from ..access_engine import can_access_cluster, can_access_namespace

    if not cluster_id or not namespace:
        return None, "clusterId and namespace are required.", 400

    if user:
        from ..access_engine import can_access_cluster as cac, can_access_namespace as can
        if not cac(user, cluster_id):
            return None, "Forbidden", 403
        if not can(user, cluster_id, namespace):
            return None, "Forbidden", 403

    if not should_use_real_k8s(cluster_id):
        return {"items": [], "count": 0}, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        output = _run_for_access(access, ["get", "pods", "-n", namespace, "-o", "json"])
        items = json.loads(output).get("items", [])
        names = sorted(
            item.get("metadata", {}).get("name", "")
            for item in items
            if item.get("metadata", {}).get("name")
        )
        return {"items": names, "count": len(names)}, None, 200
    except K8sCommandError as exc:
        return None, f"Failed to list pods: {exc}", 503
