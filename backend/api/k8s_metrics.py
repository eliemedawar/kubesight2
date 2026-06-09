from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, Union

from .cluster_access import ClusterAccess
from .k8s_provider import K8sCommandError, _cpu_to_cores, _run_kubectl

CPU_ALERT_THRESHOLD_PERCENT = float(os.getenv("ALERT_CPU_THRESHOLD_PERCENT", "80"))

PodTopKey = Tuple[str, str]
PodTopMetrics = Dict[PodTopKey, Dict[str, float]]


def _access_kwargs(access: Union[ClusterAccess, str]) -> Tuple[Optional[str], Optional[str]]:
    if isinstance(access, ClusterAccess):
        return access.context_name, access.kubeconfig_path
    return access, None


def _memory_to_mib(value: str) -> float:
    if not value:
        return 0.0
    value = value.strip()
    suffixes = (
        ("Ti", 1024 * 1024),
        ("Gi", 1024),
        ("Mi", 1),
        ("Ki", 1 / 1024),
        ("T", 1024 * 1024 * 1000 / (1024 * 1024)),
        ("G", 1024 * 1000 / (1024 * 1024)),
        ("M", 1000 / (1024 * 1024)),
        ("K", 1000 / (1024 * 1024)),
    )
    for suffix, multiplier in suffixes:
        if value.endswith(suffix):
            try:
                return round(float(value[: -len(suffix)]) * multiplier, 1)
            except ValueError:
                return 0.0
    try:
        return round(float(value) / (1024 * 1024), 1)
    except ValueError:
        return 0.0


def format_k8s_age(creation_timestamp: Optional[str]) -> str:
    if not creation_timestamp:
        return "-"
    try:
        created = datetime.fromisoformat(creation_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    seconds = int((datetime.now(timezone.utc) - created).total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 365:
        return f"{days}d"
    return f"{days // 365}y"


def fetch_pod_top_metrics(access: Union[ClusterAccess, str]) -> PodTopMetrics:
    """Return {(namespace, pod_name): {cpu: cores, memory: mib}} from kubectl top."""
    context_name, kubeconfig_path = _access_kwargs(access)
    try:
        output = _run_kubectl(
            ["top", "pods", "-A", "--no-headers"],
            context=context_name,
            kubeconfig_path=kubeconfig_path,
        )
    except K8sCommandError:
        return {}

    usage: PodTopMetrics = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        namespace, pod_name = parts[0], parts[1]
        metrics: Dict[str, float] = {"cpu": _cpu_to_cores(parts[2])}
        if len(parts) >= 4:
            metrics["memory"] = _memory_to_mib(parts[3])
        usage[(namespace, pod_name)] = metrics
    return usage


def fetch_pod_cpu_usage_cores(access: Union[ClusterAccess, str]) -> Dict[PodTopKey, float]:
    return {
        key: values["cpu"]
        for key, values in fetch_pod_top_metrics(access).items()
        if "cpu" in values
    }


def pod_total_cpu_limit_cores(pod: dict) -> float:
    total = 0.0
    spec = pod.get("spec", {})
    containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
    for container in containers:
        limits = (container.get("resources") or {}).get("limits") or {}
        cpu_limit = limits.get("cpu")
        if cpu_limit:
            total += _cpu_to_cores(str(cpu_limit))
    return total


def pod_total_memory_limit_mib(pod: dict) -> float:
    total = 0.0
    spec = pod.get("spec", {})
    containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
    for container in containers:
        limits = (container.get("resources") or {}).get("limits") or {}
        memory_limit = limits.get("memory")
        if memory_limit:
            total += _memory_to_mib(str(memory_limit))
    return total


def cpu_usage_percent(usage_cores: float, limit_cores: float) -> float:
    if limit_cores <= 0:
        return 0.0
    return round((usage_cores / limit_cores) * 100, 1)


def _sum_pod_top_usage(access: Union[ClusterAccess, str]) -> Tuple[float, float]:
    top = fetch_pod_top_metrics(access)
    cpu = sum(metrics.get("cpu", 0.0) for metrics in top.values())
    memory_mib = sum(metrics.get("memory", 0.0) for metrics in top.values())
    return cpu, memory_mib


def aggregate_pod_top_by_namespace(access: Union[ClusterAccess, str]) -> Dict[str, Dict[str, float]]:
    """Return {namespace: {cpu: cores, memory_mib: mib}} summed from kubectl top pods."""
    totals: Dict[str, Dict[str, float]] = {}
    for (namespace, _), metrics in fetch_pod_top_metrics(access).items():
        entry = totals.setdefault(namespace, {"cpu": 0.0, "memory_mib": 0.0})
        entry["cpu"] += metrics.get("cpu", 0.0)
        entry["memory_mib"] += metrics.get("memory", 0.0)
    return totals


def fetch_node_top_usage(access: Union[ClusterAccess, str]) -> Tuple[float, float]:
    """Return (cpu_cores, memory_mib) summed across nodes from kubectl top nodes."""
    context_name, kubeconfig_path = _access_kwargs(access)
    try:
        output = _run_kubectl(
            ["top", "nodes", "--no-headers"],
            context=context_name,
            kubeconfig_path=kubeconfig_path,
        )
    except K8sCommandError:
        return 0.0, 0.0

    cpu_cores = 0.0
    memory_mib = 0.0
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        cpu_cores += _cpu_to_cores(parts[1])
        memory_mib += _memory_to_mib(parts[3])
    return cpu_cores, memory_mib


def metrics_server_available(access: Union[ClusterAccess, str]) -> bool:
    context_name, kubeconfig_path = _access_kwargs(access)
    try:
        _run_kubectl(
            ["top", "nodes", "--no-headers"],
            context=context_name,
            kubeconfig_path=kubeconfig_path,
        )
        return True
    except K8sCommandError:
        return False


def _memory_to_gib(value: str) -> float:
    return round(_memory_to_mib(value) / 1024, 2)


def fetch_node_allocatable(access: Union[ClusterAccess, str]) -> Tuple[float, float]:
    """Return (cpu_allocatable_cores, memory_allocatable_gib) from node status."""
    import json

    from .k8s_provider import _run_for_access

    if not isinstance(access, ClusterAccess):
        access = ClusterAccess(cluster_id="", context_name=access)
    output = _run_for_access(access, ["get", "nodes", "-o", "json"])
    node_items = json.loads(output).get("items", [])
    cpu_total = 0.0
    mem_gib = 0.0
    for node in node_items:
        allocatable = node.get("status", {}).get("allocatable", {})
        cpu_total += _cpu_to_cores(str(allocatable.get("cpu", "0")))
        mem_gib += _memory_to_gib(str(allocatable.get("memory", "0Gi")))
    return cpu_total, mem_gib


def cluster_utilization_metrics(access: Union[ClusterAccess, str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build CPU and memory utilization payloads using metrics-server and node allocatable."""
    from .dashboard_intelligence import build_utilization_metric, format_cpu_display, format_memory_gib

    unavailable = {
        "available": False,
        "title": "Metrics unavailable",
        "reason": "Metrics Server is not installed or accessible.",
        "helpText": "Install Metrics Server to enable utilization monitoring.",
    }

    if not metrics_server_available(access):
        return unavailable, unavailable

    try:
        alloc_cpu, alloc_mem_gib = fetch_node_allocatable(access)
        used_cpu, used_mib = fetch_node_top_usage(access)
        used_mem_gib = round(used_mib / 1024, 2)
    except K8sCommandError:
        return unavailable, unavailable

    if alloc_cpu <= 0 and alloc_mem_gib <= 0:
        no_capacity = {
            **unavailable,
            "reason": "Node allocatable capacity could not be determined.",
        }
        return no_capacity, no_capacity

    cpu_percent = round((used_cpu / alloc_cpu) * 100, 1) if alloc_cpu > 0 else None
    mem_percent = round((used_mem_gib / alloc_mem_gib) * 100, 1) if alloc_mem_gib > 0 else None

    cpu = build_utilization_metric(
        available=cpu_percent is not None and alloc_cpu > 0,
        used=round(used_cpu, 3),
        allocatable=round(alloc_cpu, 3),
        percent=cpu_percent,
        used_display=format_cpu_display(used_cpu),
        allocatable_display=format_cpu_display(alloc_cpu),
        unit="cores",
        reason="CPU allocatable capacity is not available.",
    )
    memory = build_utilization_metric(
        available=mem_percent is not None and alloc_mem_gib > 0,
        used=used_mem_gib,
        allocatable=alloc_mem_gib,
        percent=mem_percent,
        used_display=format_memory_gib(used_mem_gib),
        allocatable_display=format_memory_gib(alloc_mem_gib),
        unit="Gi",
        reason="Memory allocatable capacity is not available.",
    )

    if not cpu.get("available"):
        cpu = {**unavailable, "reason": cpu.get("reason", unavailable["reason"])}
    if not memory.get("available"):
        memory = {**unavailable, "reason": memory.get("reason", unavailable["reason"])}
    return cpu, memory


def cluster_resource_usage(
    access: Union[ClusterAccess, str],
    cpu_capacity_cores: float,
    memory_capacity_gib: float,
) -> Tuple[Optional[float], Optional[float], float, float]:
    """
    Return (cpu_usage_percent, memory_usage_percent, used_cores, used_gib).
    Uses kubectl top nodes, falling back to summed pod metrics.
    """
    used_cpu, used_mib = fetch_node_top_usage(access)
    pod_cpu, pod_mib = _sum_pod_top_usage(access)
    if used_cpu <= 0.0 and pod_cpu > 0.0:
        used_cpu = pod_cpu
    if used_mib <= 0.0 and pod_mib > 0.0:
        used_mib = pod_mib

    used_gib = round(used_mib / 1024, 2)
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None

    if cpu_capacity_cores > 0:
        cpu_percent = round((used_cpu / cpu_capacity_cores) * 100, 1)
    if memory_capacity_gib > 0:
        memory_percent = round((used_gib / memory_capacity_gib) * 100, 1)

    return cpu_percent, memory_percent, round(used_cpu, 2), used_gib
