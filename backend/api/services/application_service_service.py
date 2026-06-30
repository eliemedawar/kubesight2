from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..models import ApplicationService, ApplicationServiceDeployment, ApplicationServiceTopologyNode, ApplicationServiceTopologyEdge

# Short-lived cache for the live service list. Building it runs `kubectl` against
# every linked namespace, so reopening the page would otherwise pay that cost
# every time. The TTL keeps health reasonably fresh while making repeat opens
# (and the common navigate-away-and-back) effectively instant. Keyed per user so
# one user's namespace-filtered health never leaks to another.
_LIST_CACHE_TTL_SECONDS = int(os.getenv("APP_SERVICES_LIST_CACHE_TTL_SECONDS", "15"))
_list_services_cache: Dict[Any, Tuple[float, Dict[str, Any]]] = {}
_list_services_cache_lock = threading.Lock()


def _list_cache_disabled() -> bool:
    """Disable caching under pytest so patched kubectl/mutations aren't masked."""
    try:
        from flask import current_app

        return bool(getattr(current_app, "config", {}).get("TESTING"))
    except Exception:
        return False


def invalidate_list_services_cache() -> None:
    with _list_services_cache_lock:
        _list_services_cache.clear()

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
    if "healthy" in statuses:
        return "healthy"
    # Only "unknown" (unreachable/not-found) statuses remain.
    return "unknown"


def _mock_deployment_detail(dep: Dict[str, Any]) -> Dict[str, Any]:
    key = (dep["clusterId"], dep["namespace"], dep["deploymentName"])
    health = _MOCK_DEPLOYMENT_HEALTH.get(key, {"desired": 1, "available": 1, "ready": 1})
    desired = health["desired"]
    available = health["available"]
    ready = health["ready"]
    return {
        "id": dep["id"],
        "serviceId": dep["serviceId"],
        "clusterId": dep["clusterId"],
        "namespace": dep["namespace"],
        "deploymentName": dep["deploymentName"],
        "desiredReplicas": desired,
        "availableReplicas": available,
        "readyReplicas": ready,
        "status": _deployment_status(desired, min(available, ready)),
        "dr": None,
        "drStatus": None,
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

# Supported workload kinds for a linked resource / DR counterpart and the
# kubectl resource name + per-namespace health-map key for each. "pod" is handled
# separately (it has no replica spec).
_KIND_TO_KUBECTL = {
    "deployment": "deployments",
    "statefulset": "statefulsets",
    "daemonset": "daemonsets",
    "pod": "pods",
}
WORKLOAD_KINDS = ("deployment", "statefulset", "daemonset", "pod")


def _normalize_kind(value: Optional[str], default: str = "deployment") -> str:
    kind = (value or "").strip().lower()
    return kind if kind in WORKLOAD_KINDS else default


def _workloads_for_access(access, namespace: str, kubectl_kind: str) -> Dict[str, Dict[str, Any]]:
    """Run ``kubectl get <kind>`` for an already-resolved cluster access and return
    ``{name: raw_k8s_item}``. Subprocess/JSON only — safe in worker threads."""
    from ..k8s_provider import K8sCommandError, _run_for_access

    try:
        output = _run_for_access(access, ["get", kubectl_kind, "-n", namespace, "-o", "json"])
        items = json.loads(output).get("items", [])
        return {
            item.get("metadata", {}).get("name", ""): item
            for item in items
            if item.get("metadata", {}).get("name")
        }
    except (K8sCommandError, Exception):
        return {}


def _deployments_for_access(access, namespace: str) -> Dict[str, Dict[str, Any]]:
    """Run ``kubectl get deployments`` and return ``{deployment_name: raw_item}``."""
    return _workloads_for_access(access, namespace, "deployments")


def _fetch_namespace_deployment_map(
    cluster_id: str,
    namespace: str,
    user=None,
) -> Dict[str, Dict[str, Any]]:
    """Return {deployment_name: raw_k8s_item} for all deployments in a namespace.

    Resolves access (DB-backed) on the calling thread, so this must run inside a
    Flask app context.
    """
    from ..k8s_provider import resolve_cluster_access, should_use_real_k8s
    from ..access_engine import can_access_namespace

    if not should_use_real_k8s(cluster_id):
        return {}
    if user and not can_access_namespace(user, cluster_id, namespace):
        return {}
    access = resolve_cluster_access(cluster_id)
    if not access:
        return {}
    return _deployments_for_access(access, namespace)


# Container waiting/terminated reasons that mean a pod is genuinely broken
# (as opposed to a transient "ContainerCreating"/"PodInitializing").
_BAD_POD_REASONS = {
    "crashloopbackoff", "imagepullbackoff", "errimagepull", "errimagepullbackoff",
    "createcontainererror", "createcontainerconfigerror", "invalidimagename",
    "runcontainererror", "oomkilled",
}


def _pods_for_access(access, namespace: str) -> List[Dict[str, Any]]:
    """Run ``kubectl get pods`` for an already-resolved cluster access. Touches
    neither the DB nor the Flask app context, so it is safe in worker threads."""
    from ..k8s_provider import K8sCommandError, _run_for_access

    try:
        output = _run_for_access(access, ["get", "pods", "-n", namespace, "-o", "json"])
        return json.loads(output).get("items", [])
    except (K8sCommandError, Exception):
        return []


def _fetch_namespace_pods(cluster_id: str, namespace: str, user=None) -> List[Dict[str, Any]]:
    """Return the raw k8s pod items for a namespace (or [] if unavailable).

    Resolves access (DB-backed) on the calling thread, so this must run inside a
    Flask app context.
    """
    from ..k8s_provider import resolve_cluster_access, should_use_real_k8s
    from ..access_engine import can_access_namespace

    if not should_use_real_k8s(cluster_id):
        return []
    if user and not can_access_namespace(user, cluster_id, namespace):
        return []
    access = resolve_cluster_access(cluster_id)
    if not access:
        return []
    return _pods_for_access(access, namespace)


def _pod_is_down(pod_item: Dict[str, Any]) -> bool:
    """True if a pod is broken: failed, crash-looping, image-pull errors, or
    running-but-not-ready (failing its readiness probe)."""
    status = pod_item.get("status", {})
    phase = (status.get("phase") or "").lower()
    if phase == "succeeded":
        return False
    if phase == "failed":
        return True

    container_states = (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or [])
    for cs in container_states:
        state = cs.get("state") or {}
        waiting_reason = ((state.get("waiting") or {}).get("reason") or "").lower()
        if waiting_reason in _BAD_POD_REASONS:
            return True
        terminated_reason = ((state.get("terminated") or {}).get("reason") or "").lower()
        if terminated_reason in _BAD_POD_REASONS:
            return True

    # A running pod whose Ready condition is False is up but failing.
    if phase == "running":
        for cond in (status.get("conditions") or []):
            if cond.get("type") == "Ready":
                return cond.get("status") != "True"
    return False


def _pod_belongs_to_deployment(pod_item: Dict[str, Any], deployment_name: str) -> bool:
    """True if a pod is owned by a ReplicaSet of the given deployment. The RS name
    is ``<deployment>-<hash>``, so we require the prefix and no extra '-' after it
    (so deployment ``auth`` does not swallow pods of ``auth-api``)."""
    prefix = f"{deployment_name}-"
    for ref in pod_item.get("metadata", {}).get("ownerReferences", []) or []:
        if ref.get("kind") != "ReplicaSet":
            continue
        rs_name = ref.get("name") or ""
        if rs_name.startswith(prefix) and "-" not in rs_name[len(prefix):]:
            return True
    return False


# kind -> the ownerReference.kind that directly owns the pods (StatefulSets and
# DaemonSets own pods directly; Deployments own them via a ReplicaSet).
_DIRECT_POD_OWNER = {"statefulset": "StatefulSet", "daemonset": "DaemonSet"}


def _pod_owned_by_workload(pod_item: Dict[str, Any], kind: str, name: str) -> bool:
    """True if a pod belongs to the given workload, across kinds."""
    if kind == "deployment":
        return _pod_belongs_to_deployment(pod_item, name)
    owner_kind = _DIRECT_POD_OWNER.get(kind)
    if not owner_kind:
        return False
    for ref in pod_item.get("metadata", {}).get("ownerReferences", []) or []:
        if ref.get("kind") == owner_kind and ref.get("name") == name:
            return True
    return False


def _workload_replica_counts(kind: str, item: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Return (desired, ready, available, unavailable) for a workload object.

    DaemonSets expose scheduling counts instead of ``spec.replicas``; Deployments
    and StatefulSets both use the replicas/readyReplicas/availableReplicas shape.
    """
    spec = item.get("spec", {})
    status = item.get("status", {})
    if kind == "daemonset":
        desired = status.get("desiredNumberScheduled") or 0
        ready = status.get("numberReady") or 0
        available = status.get("numberAvailable")
        available = ready if available is None else available
        unavailable = status.get("numberUnavailable") or 0
    else:  # deployment, statefulset
        desired = spec.get("replicas") or 0
        ready = status.get("readyReplicas") or 0
        available = status.get("availableReplicas")
        available = ready if available is None else available
        unavailable = status.get("unavailableReplicas") or 0
    return desired, ready, available, unavailable


def _live_deployment_detail(dep_row, ns_data: Dict[str, Any]) -> Dict[str, Any]:
    pods = ns_data.get("pods", []) if isinstance(ns_data, dict) else []
    kind = _normalize_kind(getattr(dep_row, "resource_kind", None))

    def _result(desired, available, ready, status):
        return {
            "id": dep_row.id,
            "serviceId": dep_row.service_id,
            "clusterId": dep_row.cluster_id,
            "namespace": dep_row.namespace,
            "deploymentName": dep_row.deployment_name,
            "kind": kind,
            "desiredReplicas": desired,
            "availableReplicas": available,
            "readyReplicas": ready,
            "status": status,
        }

    # A linked pod: health is simply that pod's own state.
    if kind == "pod":
        pod = next((p for p in pods if p.get("metadata", {}).get("name") == dep_row.deployment_name), None)
        if pod is None:
            return _result(0, 0, 0, "unknown")
        down = _pod_is_down(pod)
        return _result(1, 0 if down else 1, 0 if down else 1, "critical" if down else "healthy")

    map_key = _KIND_TO_KUBECTL.get(kind, "deployments")
    items = ns_data.get(map_key, {}) if isinstance(ns_data, dict) else {}
    item = items.get(dep_row.deployment_name)
    if item is None:
        # Cluster unreachable or workload not found. Report "unknown" rather than
        # letting desired==0 fall through to "healthy", which would hide the real
        # state of the linked workload.
        return _result(0, 0, 0, "unknown")

    desired, ready, available, unavailable = _workload_replica_counts(kind, item)

    # Base status off the stricter ready count, and flag any unavailable replicas.
    status = _deployment_status(desired, min(available, ready))
    if unavailable and status == "healthy":
        status = "warning"

    # Pod-level check: catch a down/crashing pod even when replica counts look OK
    # (e.g. an extra or old pod stuck in CrashLoopBackOff).
    dep_pods = [p for p in pods if _pod_owned_by_workload(p, kind, dep_row.deployment_name)]
    down_pods = [p for p in dep_pods if _pod_is_down(p)]
    if down_pods:
        if dep_pods and len(down_pods) == len(dep_pods):
            status = "critical"
        elif status != "critical":
            status = "warning"

    return _result(desired, available, ready, status)


def _build_k8s_health_map(
    deployments: List,
    user=None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Fetch all unique (cluster, namespace) pairs in parallel and return a map of
    ``(cluster, namespace) -> {"deployments": {...}, "pods": [...]}``.

    Access resolution (``should_use_real_k8s``, ``can_access_namespace``,
    ``resolve_cluster_access``) is DB-backed and depends on the Flask app context
    and the ``user`` ORM instance, so it must happen on the calling thread. Worker
    threads run only the ``kubectl`` calls, which need neither. Doing the access
    checks inside the threads would raise ``RuntimeError: Working outside of
    application context``; that exception was previously swallowed, leaving every
    namespace empty and making each linked deployment look healthy.
    """
    from ..k8s_provider import resolve_cluster_access, should_use_real_k8s
    from ..access_engine import can_access_namespace

    # Figure out which workload kinds are actually linked in each (cluster,
    # namespace) pair. A namespace whose linked resources are all Deployments
    # should never pay for statefulset/daemonset fetches. "pods" is always
    # needed: both pod-kind links and the pod-level health check on every
    # workload read the pod list.
    needed_kinds: Dict[Tuple[str, str], set] = {}

    def _want(pair: Tuple[str, str], kind: str) -> None:
        bucket = needed_kinds.setdefault(pair, {"pods"})
        bucket.add(_KIND_TO_KUBECTL.get(kind, "deployments"))

    for d in deployments:
        _want((d.cluster_id, d.namespace), _normalize_kind(getattr(d, "resource_kind", None)))
        # DR counterparts may live on a different cluster/namespace.
        if _has_dr(d):
            _want((d.dr_cluster_id, d.dr_namespace), _normalize_kind(getattr(d, "dr_resource_kind", None)))

    result: Dict[Tuple[str, str], Dict[str, Any]] = {}

    # Resolve access on the main thread (valid app context + session).
    accesses: Dict[Tuple[str, str], Any] = {}
    for pair in needed_kinds:
        cluster_id, namespace = pair
        if not should_use_real_k8s(cluster_id):
            continue
        if user is not None and not can_access_namespace(user, cluster_id, namespace):
            continue
        access = resolve_cluster_access(cluster_id)
        if access:
            accesses[pair] = access

    if not accesses:
        return result

    # Seed every reachable pair with empty buckets so a kind we deliberately
    # skipped (because nothing links it) reads as empty rather than absent.
    for pair in accesses:
        result[pair] = {"deployments": {}, "statefulsets": {}, "daemonsets": {}, "pods": []}

    # Flatten to one task per (pair, kind) so every kubectl call runs
    # concurrently, instead of four serial calls inside each namespace's worker.
    tasks = [
        (pair, accesses[pair], kubectl_kind)
        for pair in accesses
        for kubectl_kind in needed_kinds[pair]
    ]

    def fetch(pair: Tuple[str, str], access, kubectl_kind: str):
        _, namespace = pair
        if kubectl_kind == "pods":
            return pair, "pods", _pods_for_access(access, namespace)
        return pair, kubectl_kind, _workloads_for_access(access, namespace, kubectl_kind)

    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as pool:
        futures = [pool.submit(fetch, p, a, k) for (p, a, k) in tasks]
        for fut in as_completed(futures):
            try:
                pair, bucket, data = fut.result()
                result[pair][bucket] = data
            except Exception:
                pass
    return result


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _has_dr(dep: ApplicationServiceDeployment) -> bool:
    """True when a linked component has a fully-configured DR counterpart."""
    return bool(
        getattr(dep, "dr_cluster_id", None)
        and getattr(dep, "dr_namespace", None)
        and getattr(dep, "dr_resource_name", None)
    )


def _dr_config(dep: ApplicationServiceDeployment) -> Optional[Dict[str, Any]]:
    if not _has_dr(dep):
        return None
    return {
        "clusterId": dep.dr_cluster_id,
        "namespace": dep.dr_namespace,
        "deploymentName": dep.dr_resource_name,
        "kind": dep.dr_resource_kind or "deployment",
    }


def _dr_target_row(dep: ApplicationServiceDeployment) -> SimpleNamespace:
    """A lightweight stand-in row representing the DR counterpart, so it can be
    fed to :func:`_live_deployment_detail` to compute live DR health."""
    return SimpleNamespace(
        id=dep.id,
        service_id=dep.service_id,
        cluster_id=dep.dr_cluster_id,
        namespace=dep.dr_namespace,
        deployment_name=dep.dr_resource_name,
        resource_kind=dep.dr_resource_kind or "deployment",
    )


def _attach_dr(
    dep_detail: Dict[str, Any],
    dep_row: ApplicationServiceDeployment,
    k8s_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]],
) -> Dict[str, Any]:
    """Annotate a primary component's detail with its DR config + live DR status."""
    config = _dr_config(dep_row)
    dep_detail["dr"] = config
    if not config:
        dep_detail["drStatus"] = None
        return dep_detail
    if k8s_map is not None:
        dr_detail = _live_deployment_detail(
            _dr_target_row(dep_row),
            k8s_map.get((dep_row.dr_cluster_id, dep_row.dr_namespace), {}),
        )
        dep_detail["drStatus"] = dr_detail["status"]
        dep_detail["drDetail"] = dr_detail
    else:
        dep_detail["drStatus"] = "unknown"
    return dep_detail


def _dr_fields_from_payload(dep: Dict[str, Any], primary_kind: str) -> Dict[str, Any]:
    """Extract DR counterpart columns from a linked-resource payload.

    Accepts either a nested ``dr`` object or flat ``drClusterId`` / ``drNamespace``
    / ``drResourceName`` / ``drResourceKind`` keys. Returns all-None when DR is not
    fully specified, so a partially-filled DR link is simply ignored.
    """
    dr = dep.get("dr") if isinstance(dep.get("dr"), dict) else {}
    cluster_id = (dr.get("clusterId") or dep.get("drClusterId") or "").strip()
    namespace = (dr.get("namespace") or dep.get("drNamespace") or "").strip()
    name = (dr.get("deploymentName") or dep.get("drResourceName") or "").strip()
    kind = _normalize_kind(dr.get("kind") or dep.get("drResourceKind"), default=_normalize_kind(primary_kind))
    if not (cluster_id and namespace and name):
        return {"dr_cluster_id": None, "dr_namespace": None, "dr_resource_name": None, "dr_resource_kind": None}
    return {
        "dr_cluster_id": cluster_id,
        "dr_namespace": namespace,
        "dr_resource_name": name,
        "dr_resource_kind": kind,
    }


def _dep_to_dict(dep: ApplicationServiceDeployment) -> Dict[str, Any]:
    return {
        "id": dep.id,
        "serviceId": dep.service_id,
        "clusterId": dep.cluster_id,
        "namespace": dep.namespace,
        "deploymentName": dep.deployment_name,
        "kind": getattr(dep, "resource_kind", None) or "deployment",
        "dr": _dr_config(dep),
        "createdAt": dep.created_at.isoformat() if dep.created_at else None,
    }


def _node_component_fields(node) -> Dict[str, Any]:
    """Resolve a node's predefined-component link (id + live status) for the UI."""
    component = getattr(node, "component", None)
    if not node.component_id or not component:
        return {"componentId": node.component_id, "component": None, "componentStatus": None}
    from .topology_component_service import component_summary

    return {
        "componentId": node.component_id,
        "component": component_summary(component),
        "componentStatus": component.last_status or "unknown",
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
                "positionX": n.position_x,
                "positionY": n.position_y,
                **_node_component_fields(n),
            }
            for n in nodes
        ],
        "edges": [
            {
                "id": e.id,
                "sourceNodeId": e.source_node_id,
                "targetNodeId": e.target_node_id,
                "protocol": e.protocol,
                "scope": e.scope,
                "description": e.description,
            }
            for e in edges
        ],
    }


# A predefined component's health uses healthy/degraded/unhealthy/unknown; the
# service health uses healthy/warning/critical/unknown. Map so that an unhealthy
# component drags the whole service to "critical" and a degraded one to "warning".
_COMPONENT_TO_SERVICE_HEALTH = {
    "healthy": "healthy",
    "degraded": "warning",
    "unhealthy": "critical",
    "unknown": "unknown",
}


def _service_to_dict(
    svc: ApplicationService,
    deployment_details: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    deps = deployment_details if deployment_details is not None else [_dep_to_dict(d) for d in svc.deployments]
    topology = _topology_to_dict(svc)

    # Overall service health combines linked-workload health with the health of
    # any predefined components dropped into the topology — so an unhealthy
    # component makes the whole service unhealthy.
    statuses = [d.get("status", "unknown") for d in deps if "status" in d]
    component_statuses = [
        _COMPONENT_TO_SERVICE_HEALTH.get(node["componentStatus"], "unknown")
        for node in topology["nodes"]
        if node.get("componentStatus")
    ]
    combined = statuses + component_statuses
    health = _aggregate_health(combined) if combined else "unknown"

    # DR health: aggregate of every configured DR counterpart's status. None when
    # no component on this service has a DR counterpart linked.
    dr_statuses = [d.get("drStatus") for d in deps if d.get("drStatus")]
    dr_health = _aggregate_health(dr_statuses) if dr_statuses else None
    return {
        "id": svc.id,
        "name": svc.name,
        "description": svc.description or "",
        "deploymentCount": len(svc.deployments),
        "deployments": deps,
        "health": health,
        "componentHealth": _aggregate_health(component_statuses) if component_statuses else None,
        "hasDr": bool(dr_statuses),
        "drHealth": dr_health,
        "topology": topology,
        "createdAt": svc.created_at.isoformat() if svc.created_at else None,
        "updatedAt": svc.updated_at.isoformat() if svc.updated_at else None,
    }


def _build_deployment_details(
    svc: ApplicationService,
    k8s_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Per-component detail dicts with both primary health and DR annotations."""
    details = []
    for dep in svc.deployments:
        if k8s_map is not None:
            detail = _live_deployment_detail(dep, k8s_map.get((dep.cluster_id, dep.namespace), {}))
        else:
            detail = _dep_to_dict(dep)
        _attach_dr(detail, dep, k8s_map)
        details.append(detail)
    return details


def _service_list_item(
    svc: ApplicationService,
    k8s_health_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    user=None,
) -> Dict[str, Any]:
    deps = _build_deployment_details(svc, k8s_health_map)
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

    def _coerce_pos(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _coerce_int_or_none(value):
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # Resolve which predefined component ids actually exist, so a stale id in the
    # payload never produces a dangling FK.
    from ..models import TopologyComponent

    def _resolve_component_id(raw):
        cid = _coerce_int_or_none(raw)
        if cid is None:
            return None
        return cid if TopologyComponent.query.get(cid) else None

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
            component_id=_resolve_component_id(node_data.get("componentId")),
            position_x=_coerce_pos(node_data.get("positionX")),
            position_y=_coerce_pos(node_data.get("positionY")),
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
        protocol = (edge_data.get("protocol") or "").strip()[:20] or None
        scope = (edge_data.get("scope") or "").strip().lower()[:20] or None
        if scope not in ("internal", "external"):
            scope = None
        description = (edge_data.get("description") or "").strip()[:1000] or None
        db.session.add(ApplicationServiceTopologyEdge(
            service_id=service_id,
            source_node_id=src_id,
            target_node_id=tgt_id,
            protocol=protocol,
            scope=scope,
            description=description,
        ))


# ---------------------------------------------------------------------------
# Public CRUD API
# ---------------------------------------------------------------------------

def list_services(user=None) -> Dict[str, Any]:
    cache_enabled = not _list_cache_disabled()
    cache_key = getattr(user, "id", None) if user else "anon"
    if cache_enabled:
        now_ts = time.time()
        with _list_services_cache_lock:
            cached = _list_services_cache.get(cache_key)
        if cached and cached[0] > now_ts:
            return cached[1]

    services = ApplicationService.query.order_by(ApplicationService.name.asc()).all()

    # Collect all deployment rows and fetch live health if in real mode. DR
    # counterparts may live on a different cluster, so consider those too.
    all_deployments = [d for svc in services for d in svc.deployments]
    k8s_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None
    if all_deployments and _any_real_cluster(all_deployments):
        k8s_map = _build_k8s_health_map(all_deployments, user=user)

    items = [_service_list_item(svc, k8s_health_map=k8s_map, user=user) for svc in services]
    payload = {"items": items, "count": len(items)}

    if cache_enabled:
        with _list_services_cache_lock:
            _list_services_cache[cache_key] = (time.time() + _LIST_CACHE_TTL_SECONDS, payload)
    return payload


def _any_real_cluster(deployments: List[ApplicationServiceDeployment]) -> bool:
    from ..k8s_provider import should_use_real_k8s

    for d in deployments:
        if should_use_real_k8s(d.cluster_id):
            return True
        if _has_dr(d) and should_use_real_k8s(d.dr_cluster_id):
            return True
    return False


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
            "hasDr": False,
            "drHealth": None,
            "topology": svc.get("topology", {"nodes": [], "edges": []}),
            "createdAt": svc["createdAt"],
            "updatedAt": svc["updatedAt"],
        })
    return {"items": items, "count": len(items)}


def get_service(service_id: int, user=None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    svc = ApplicationService.query.get(service_id)
    if not svc:
        return None, "Application service not found", 404

    k8s_map: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None
    if svc.deployments and _any_real_cluster(list(svc.deployments)):
        k8s_map = _build_k8s_health_map(list(svc.deployments), user=user)

    deps = _build_deployment_details(svc, k8s_map)
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
        "hasDr": False,
        "drHealth": None,
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
        resource_kind = _normalize_kind(dep.get("kind"))
        if not (cluster_id and namespace and deployment_name):
            continue
        key = (cluster_id, namespace, deployment_name, resource_kind)
        if key in seen:
            continue
        seen.add(key)
        dr = _dr_fields_from_payload(dep, resource_kind)
        db.session.add(ApplicationServiceDeployment(
            service_id=svc.id,
            cluster_id=cluster_id,
            namespace=namespace,
            deployment_name=deployment_name,
            resource_kind=resource_kind,
            **dr,
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
    invalidate_list_services_cache()
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
        resource_kind = _normalize_kind(dep.get("kind"))
        if not (cluster_id and namespace and deployment_name):
            continue
        key = (cluster_id, namespace, deployment_name, resource_kind)
        if key in seen:
            continue
        seen.add(key)
        dr = _dr_fields_from_payload(dep, resource_kind)
        db.session.add(ApplicationServiceDeployment(
            service_id=svc.id,
            cluster_id=cluster_id,
            namespace=namespace,
            deployment_name=deployment_name,
            resource_kind=resource_kind,
            **dr,
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
    invalidate_list_services_cache()
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
    # If this service was created by Deploy From Blueprint, remove the linked
    # blueprint instance (AppService + its component mappings) too, so the
    # Service Catalog's "deployed" count stays in sync.
    from ..models import AppService

    for instance in AppService.query.filter_by(application_service_id=service_id).all():
        db.session.delete(instance)

    db.session.delete(svc)
    db.session.commit()
    log_audit(
        "app_service_deleted",
        actor_user_id=actor_user_id,
        target_type="application_service",
        target_id=str(service_id),
        details={"name": name},
    )
    invalidate_list_services_cache()
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


# ---------------------------------------------------------------------------
# Generic workload picker — list names of any supported kind in a namespace
# (Deployment / StatefulSet / DaemonSet / Pod), respecting RBAC.
# ---------------------------------------------------------------------------

def list_picker_workloads(
    cluster_id: str,
    namespace: str,
    kind: str,
    user=None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s

    if not cluster_id or not namespace:
        return None, "clusterId and namespace are required.", 400

    normalized = _normalize_kind(kind)
    kubectl_kind = _KIND_TO_KUBECTL.get(normalized, "deployments")

    if user:
        from ..access_engine import can_access_cluster as cac, can_access_namespace as can
        if not cac(user, cluster_id):
            return None, "Forbidden", 403
        if not can(user, cluster_id, namespace):
            return None, "Forbidden", 403

    if not should_use_real_k8s(cluster_id):
        # Only deployments have mock names; other kinds are empty in demo mode.
        if normalized == "deployment":
            names = sorted({
                dep_name
                for (cid, ns, dep_name) in _MOCK_DEPLOYMENT_HEALTH
                if cid == cluster_id and ns == namespace
            })
            return {"items": names, "count": len(names)}, None, 200
        return {"items": [], "count": 0}, None, 200

    access = resolve_cluster_access(cluster_id)
    if not access:
        return None, "Cluster not found", 404

    try:
        output = _run_for_access(access, ["get", kubectl_kind, "-n", namespace, "-o", "json"])
        items = json.loads(output).get("items", [])
        names = sorted(
            item.get("metadata", {}).get("name", "")
            for item in items
            if item.get("metadata", {}).get("name")
        )
        return {"items": names, "count": len(names)}, None, 200
    except K8sCommandError as exc:
        return None, f"Failed to list {kubectl_kind}: {exc}", 503
