"""Single-pass Kubernetes reads for the dashboard summary endpoint."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

from .cluster_access import ClusterAccess
from .k8s_metrics import PodTopMetrics, cluster_utilization_metrics_from_nodes, fetch_node_top_usage, fetch_pod_top_metrics
from .k8s_provider import (
    _node_capacity_totals,
    _run_for_access,
    build_namespaces_from_data,
    list_cpu_alerts_from_pod_data,
)
from .upgrade_provider import build_cluster_info


@dataclass(frozen=True)
class DashboardK8sSnapshot:
    node_items: List[Dict[str, Any]]
    pod_items: List[Dict[str, Any]]
    version_data: Dict[str, Any]
    namespaces: List[Dict[str, Any]]
    node_top_cpu: float
    node_top_mib: float
    pod_top: PodTopMetrics


def _safe_json_items(future, label: str) -> List[Dict[str, Any]]:
    try:
        return json.loads(future.result()).get("items", [])
    except Exception:
        logger.warning("dashboard snapshot: failed to load %s for cluster", label, exc_info=True)
        return []


def _safe_json_object(future) -> Dict[str, Any]:
    try:
        payload = json.loads(future.result())
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _strip_pod(pod: Dict[str, Any]) -> Dict[str, Any]:
    """Drop kubectl-internal fields that bloat the pod object without being used."""
    meta = pod.get("metadata")
    if meta and "managedFields" in meta:
        pod = {**pod, "metadata": {k: v for k, v in meta.items() if k != "managedFields"}}
    return pod


def fetch_dashboard_k8s_snapshot(access: ClusterAccess) -> DashboardK8sSnapshot:
    """Fetch all cluster data in one parallel batch with no redundant kubectl calls.

    Namespaces, deployments, and services are fetched alongside pods so that
    the namespace summary can be built purely from in-memory data — no second
    kubectl get pods or kubectl top pods invocation.
    """
    with ThreadPoolExecutor(max_workers=8) as pool:
        nodes_future = pool.submit(_run_for_access, access, ["get", "nodes", "-o", "json"])
        pods_future = pool.submit(
            _run_for_access, access, ["get", "pods", "--all-namespaces", "-o", "json"]
        )
        version_future = pool.submit(_run_for_access, access, ["version", "-o", "json"])
        ns_raw_future = pool.submit(_run_for_access, access, ["get", "namespaces", "-o", "json"])
        dep_future = pool.submit(_run_for_access, access, ["get", "deployments", "-A", "-o", "json"])
        svc_future = pool.submit(_run_for_access, access, ["get", "services", "-A", "-o", "json"])
        node_top_future = pool.submit(fetch_node_top_usage, access)
        # fetch_pod_top_metrics is cached (15s TTL) — safe to call from multiple futures
        pod_top_future = pool.submit(fetch_pod_top_metrics, access)

        node_items = _safe_json_items(nodes_future, "nodes")
        pod_items = [_strip_pod(p) for p in _safe_json_items(pods_future, "pods")]
        version_data = _safe_json_object(version_future)
        namespaces_raw = _safe_json_items(ns_raw_future, "namespaces")
        deployments_raw = _safe_json_items(dep_future, "deployments")
        services_raw = _safe_json_items(svc_future, "services")

        try:
            node_top_cpu, node_top_mib = node_top_future.result()
        except Exception:
            node_top_cpu, node_top_mib = 0.0, 0.0
        try:
            pod_top = pod_top_future.result()
        except Exception:
            pod_top = {}

    # Build namespace summary in memory — zero additional kubectl calls
    namespaces = build_namespaces_from_data(
        namespaces_raw, pod_items, deployments_raw, services_raw, pod_top
    ).get("items", [])

    return DashboardK8sSnapshot(
        node_items=node_items,
        pod_items=pod_items,
        version_data=version_data,
        namespaces=namespaces,
        node_top_cpu=node_top_cpu,
        node_top_mib=node_top_mib,
        pod_top=pod_top,
    )


def _resolve_usage_totals(snapshot: DashboardK8sSnapshot) -> Tuple[float, float, float, float]:
    cpu_capacity, mem_capacity = _node_capacity_totals(snapshot.node_items)
    used_cpu = snapshot.node_top_cpu
    used_mib = snapshot.node_top_mib
    if used_cpu <= 0.0 or used_mib <= 0.0:
        pod_cpu = sum(metrics.get("cpu", 0.0) for metrics in snapshot.pod_top.values())
        pod_mib = sum(metrics.get("memory", 0.0) for metrics in snapshot.pod_top.values())
        if used_cpu <= 0.0 and pod_cpu > 0.0:
            used_cpu = pod_cpu
        if used_mib <= 0.0 and pod_mib > 0.0:
            used_mib = pod_mib
    used_gib = round(used_mib / 1024, 2)
    return cpu_capacity, mem_capacity, used_cpu, used_gib


def overview_from_snapshot(access: ClusterAccess, snapshot: DashboardK8sSnapshot) -> Dict[str, Any]:
    pod_items = snapshot.pod_items
    running = sum(1 for pod in pod_items if pod.get("status", {}).get("phase") == "Running")
    pending = sum(1 for pod in pod_items if pod.get("status", {}).get("phase") == "Pending")
    failed = sum(1 for pod in pod_items if pod.get("status", {}).get("phase") == "Failed")

    cpu_capacity, mem_capacity, used_cpu, used_gib = _resolve_usage_totals(snapshot)

    return {
        "clusterId": access.cluster_id,
        "healthScore": 100 if failed == 0 else max(65, 100 - failed * 5),
        "workloads": {"deployments": 0, "statefulsets": 0, "daemonsets": 0},
        "resources": {
            "cpu": {"usedCores": used_cpu, "capacityCores": round(cpu_capacity, 2)},
            "memory": {"usedGiB": used_gib, "capacityGiB": round(mem_capacity, 2)},
            "storage": {"usedGiB": 0, "capacityGiB": 0},
        },
        "pods": {"running": running, "pending": pending, "failed": failed},
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


def cluster_info_from_snapshot(
    access: ClusterAccess,
    snapshot: DashboardK8sSnapshot,
) -> Dict[str, Any]:
    return build_cluster_info(
        access,
        _run_for_access,
        version_data=snapshot.version_data,
        node_items=snapshot.node_items,
    )


def alerts_from_snapshot(
    access: ClusterAccess,
    cluster_id: str,
    snapshot: DashboardK8sSnapshot,
) -> List[Dict[str, Any]]:
    return list_cpu_alerts_from_pod_data(
        access,
        cluster_id,
        snapshot.pod_items,
        snapshot.pod_top,
    )


def utilization_from_snapshot(snapshot: DashboardK8sSnapshot) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    return cluster_utilization_metrics_from_nodes(
        snapshot.node_items,
        snapshot.node_top_cpu,
        snapshot.node_top_mib,
    )
