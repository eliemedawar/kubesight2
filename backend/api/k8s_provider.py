from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cluster_access import ClusterAccess, custom_cluster_public_id, is_custom_cluster_id, parse_custom_cluster_db_id


class K8sCommandError(RuntimeError):
    pass


def _is_true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _kubectl_has_contexts() -> bool:
    try:
        output = _run_kubectl(["config", "get-contexts", "-o", "name"])
        return bool(output.strip())
    except K8sCommandError:
        return False


def _has_active_custom_clusters() -> bool:
    try:
        from .cluster_store import list_active_custom_clusters

        return bool(list_active_custom_clusters())
    except Exception:
        return False


def is_real_mode_enabled() -> bool:
    value = os.getenv("K8S_REAL_MODE", "auto").strip().lower()
    if value in {"0", "false", "no", "off", "mock"}:
        return _has_active_custom_clusters()
    if _is_true(value):
        return True
    if _has_active_custom_clusters():
        return True
    return _kubectl_has_contexts()


def should_use_real_k8s(cluster_id: Optional[str] = None) -> bool:
    if cluster_id and is_custom_cluster_id(cluster_id):
        from .cluster_store import get_active_cluster_by_public_id

        return get_active_cluster_by_public_id(cluster_id) is not None
    return is_real_mode_enabled()


def _run_kubectl(
    args: List[str],
    context: Optional[str] = None,
    kubeconfig_path: Optional[str] = None,
) -> str:
    command = ["kubectl"]
    if kubeconfig_path:
        command += ["--kubeconfig", kubeconfig_path]
    if context:
        command += ["--context", context]
    command += args

    env = os.environ.copy()
    if kubeconfig_path:
        env["KUBECONFIG"] = kubeconfig_path
    elif not env.get("KUBECONFIG") and env.get("K8S_KUBECONFIG"):
        env["KUBECONFIG"] = env["K8S_KUBECONFIG"]

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise K8sCommandError(stderr or f"kubectl command failed: {' '.join(command)}")
    return completed.stdout


def _run_for_access(access: ClusterAccess, args: List[str]) -> str:
    return _run_kubectl(args, context=access.context_name, kubeconfig_path=access.kubeconfig_path)


def _context_to_cluster_id(context_name: str) -> str:
    return context_name.replace("/", "-").replace("_", "-")


def _to_gib(value: str) -> float:
    # Handles Ki/Mi/Gi/Ti units (very lightweight conversion for overview values).
    if not value:
        return 0.0
    value = value.strip()
    if value.endswith("Ki"):
        return round(float(value[:-2]) / (1024 * 1024), 2)
    if value.endswith("Mi"):
        return round(float(value[:-2]) / 1024, 2)
    if value.endswith("Gi"):
        return round(float(value[:-2]), 2)
    if value.endswith("Ti"):
        return round(float(value[:-2]) * 1024, 2)
    try:
        return round(float(value), 2)
    except ValueError:
        return 0.0


def _cpu_to_cores(value: str) -> float:
    if not value:
        return 0.0
    value = value.strip()
    if value.endswith("m"):
        return round(float(value[:-1]) / 1000, 3)
    try:
        return round(float(value), 3)
    except ValueError:
        return 0.0


def _node_capacity_totals(node_items: List[Dict[str, Any]]) -> tuple[float, float]:
    cpu_capacity = 0.0
    mem_capacity = 0.0
    for node in node_items:
        capacity = node.get("status", {}).get("capacity", {})
        cpu_capacity += _cpu_to_cores(capacity.get("cpu", "0"))
        mem_capacity += _to_gib(capacity.get("memory", "0Gi"))
    return cpu_capacity, mem_capacity


def _discovered_cluster_from_context(context_name: str, now: str) -> Dict[str, Any]:
    cluster_id = _context_to_cluster_id(context_name)
    version = "unknown"
    nodes = 0
    status = "healthy"
    cpu_usage_percent: Optional[float] = None
    memory_usage_percent: Optional[float] = None
    access = ClusterAccess(
        cluster_id=cluster_id,
        context_name=context_name,
        kubeconfig_path=None,
        display_name=context_name,
        is_custom=False,
    )
    try:
        version_output = _run_for_access(access, ["version", "-o", "json"])
        version_data = json.loads(version_output)
        version = (
            version_data.get("serverVersion", {}).get("gitVersion")
            or version_data.get("serverVersion", {}).get("major", "unknown")
        )
    except Exception:
        status = "warning"

    node_items: List[Dict[str, Any]] = []
    try:
        nodes_output = _run_for_access(access, ["get", "nodes", "-o", "json"])
        nodes_data = json.loads(nodes_output)
        node_items = nodes_data.get("items", [])
        nodes = len(node_items)
        if any(
            not any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in node.get("status", {}).get("conditions", [])
            )
            for node in node_items
        ):
            status = "warning"
        if node_items:
            from .k8s_metrics import cluster_resource_usage

            cpu_capacity, mem_capacity = _node_capacity_totals(node_items)
            cpu_usage_percent, memory_usage_percent, _, _ = cluster_resource_usage(
                access, cpu_capacity, mem_capacity
            )
    except Exception:
        status = "warning"

    return {
        "id": cluster_id,
        "name": context_name,
        "provider": "kubernetes",
        "region": "unknown",
        "status": status,
        "k8sVersion": version,
        "nodes": nodes,
        "cpuUsage": cpu_usage_percent,
        "memoryUsage": memory_usage_percent,
        "lastSync": now,
        "contextName": context_name,
        "source": "kubeconfig",
    }


def _discovered_clusters_from_k8s() -> List[Dict[str, Any]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    contexts_output = _run_kubectl(["config", "get-contexts", "-o", "name"])
    contexts = [line.strip() for line in contexts_output.splitlines() if line.strip()]
    if not contexts:
        return []

    now = datetime.now(timezone.utc).isoformat()
    items: List[Dict[str, Any]] = []
    max_workers = min(4, len(contexts))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_discovered_cluster_from_context, context_name, now): context_name
            for context_name in contexts
        }
        for future in as_completed(futures):
            try:
                items.append(future.result())
            except Exception:
                context_name = futures[future]
                items.append(
                    {
                        "id": _context_to_cluster_id(context_name),
                        "name": context_name,
                        "provider": "kubernetes",
                        "region": "unknown",
                        "status": "warning",
                        "k8sVersion": "unknown",
                        "nodes": 0,
                        "cpuUsage": None,
                        "memoryUsage": None,
                        "lastSync": now,
                        "contextName": context_name,
                        "source": "kubeconfig",
                    }
                )

    items.sort(key=lambda item: item.get("name") or item.get("id") or "")
    return items


def _custom_clusters_as_items() -> List[Dict[str, Any]]:
    from .cluster_store import list_active_custom_clusters

    items: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    for cluster in list_active_custom_clusters():
        public_id = custom_cluster_public_id(cluster.id)
        status = "healthy"
        if cluster.last_connection_status == "error":
            status = "warning"
        elif cluster.last_connection_status == "connected":
            status = "healthy"
        else:
            status = "unknown"

        version = "unknown"
        nodes = 0
        cpu_usage_percent: Optional[float] = None
        memory_usage_percent: Optional[float] = None
        kubeconfig_path = cluster.kubeconfig_path
        if kubeconfig_path and Path(kubeconfig_path).is_file():
            access = ClusterAccess(
                cluster_id=public_id,
                context_name=cluster.context_name,
                kubeconfig_path=kubeconfig_path,
                display_name=cluster.name,
                is_custom=True,
            )
            try:
                version_output = _run_for_access(access, ["version", "-o", "json"])
                version_data = json.loads(version_output)
                version = (
                    version_data.get("serverVersion", {}).get("gitVersion")
                    or version_data.get("serverVersion", {}).get("major", "unknown")
                )
            except Exception:
                status = "warning"
            try:
                nodes_output = _run_for_access(access, ["get", "nodes", "-o", "json"])
                node_items = json.loads(nodes_output).get("items", [])
                nodes = len(node_items)
                if node_items:
                    from .k8s_metrics import cluster_resource_usage

                    cpu_capacity, mem_capacity = _node_capacity_totals(node_items)
                    cpu_usage_percent, memory_usage_percent, _, _ = cluster_resource_usage(
                        access, cpu_capacity, mem_capacity
                    )
            except Exception:
                status = "warning"

        items.append(
            {
                "id": public_id,
                "name": cluster.name,
                "provider": "custom",
                "region": f"{cluster.protocol}://{cluster.host}:{cluster.port}",
                "status": status,
                "k8sVersion": version,
                "nodes": nodes,
                "cpuUsage": cpu_usage_percent,
                "memoryUsage": memory_usage_percent,
                "lastSync": cluster.last_tested_at.isoformat() if cluster.last_tested_at else now,
                "contextName": cluster.context_name,
                "host": cluster.host,
                "port": cluster.port,
                "protocol": cluster.protocol,
                "source": "custom",
            }
        )
    return items


_CLUSTER_LIST_CACHE_TTL_SECONDS = 30
_cluster_list_cache: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_cluster_list_cache_lock = threading.Lock()


def invalidate_cluster_list_cache() -> None:
    """Drop cached GET /api/clusters payload after custom cluster mutations."""
    with _cluster_list_cache_lock:
        _cluster_list_cache["payload"] = None
        _cluster_list_cache["expires_at"] = 0.0


def _cluster_list_cache_disabled() -> bool:
    try:
        from flask import current_app

        return bool(getattr(current_app, "config", {}).get("TESTING"))
    except Exception:
        return False


def list_clusters_from_k8s() -> Dict[str, Any]:
    if not _cluster_list_cache_disabled():
        now_ts = time.time()
        with _cluster_list_cache_lock:
            cached = _cluster_list_cache.get("payload")
            expires_at = float(_cluster_list_cache.get("expires_at") or 0)
        if cached and expires_at > now_ts:
            return cached

    discovered: List[Dict[str, Any]] = []
    if is_real_mode_enabled() or _kubectl_has_contexts():
        try:
            discovered = _discovered_clusters_from_k8s()
        except K8sCommandError:
            discovered = []
    custom_items = _custom_clusters_as_items()
    items = discovered + custom_items
    payload = {"items": items, "count": len(items)}

    if not _cluster_list_cache_disabled():
        with _cluster_list_cache_lock:
            _cluster_list_cache["payload"] = payload
            _cluster_list_cache["expires_at"] = time.time() + _CLUSTER_LIST_CACHE_TTL_SECONDS

    return payload


def resolve_cluster_access(cluster_id: str) -> Optional[ClusterAccess]:
    if is_custom_cluster_id(cluster_id):
        from .cluster_store import get_active_cluster_by_public_id

        cluster = get_active_cluster_by_public_id(cluster_id)
        if not cluster or not cluster.kubeconfig_path:
            return None
        return ClusterAccess(
            cluster_id=custom_cluster_public_id(cluster.id),
            context_name=cluster.context_name,
            kubeconfig_path=cluster.kubeconfig_path,
            display_name=cluster.name,
            is_custom=True,
        )

    try:
        contexts_output = _run_kubectl(["config", "get-contexts", "-o", "name"])
        contexts = [line.strip() for line in contexts_output.splitlines() if line.strip()]
    except K8sCommandError:
        contexts = []

    for context_name in contexts:
        if _context_to_cluster_id(context_name) == cluster_id:
            return ClusterAccess(
                cluster_id=cluster_id,
                context_name=context_name,
                kubeconfig_path=None,
                display_name=context_name,
                is_custom=False,
            )

    for cluster in list_clusters_from_k8s().get("items", []):
        if cluster.get("id") == cluster_id and cluster.get("source") != "custom":
            return ClusterAccess(
                cluster_id=cluster_id,
                context_name=cluster.get("contextName") or cluster.get("name"),
                kubeconfig_path=None,
                display_name=cluster.get("name"),
                is_custom=False,
            )
    return None


def resolve_context_for_cluster(cluster_id: str) -> Optional[str]:
    access = resolve_cluster_access(cluster_id)
    return access.context_name if access else None


def cluster_overview_from_k8s(access: ClusterAccess) -> Dict[str, Any]:
    nodes_output = _run_for_access(access, ["get", "nodes", "-o", "json"])
    nodes_data = json.loads(nodes_output)
    node_items = nodes_data.get("items", [])

    pods_output = _run_for_access(access, ["get", "pods", "--all-namespaces", "-o", "json"])
    pods_data = json.loads(pods_output)
    pod_items = pods_data.get("items", [])

    running = sum(1 for pod in pod_items if pod.get("status", {}).get("phase") == "Running")
    pending = sum(1 for pod in pod_items if pod.get("status", {}).get("phase") == "Pending")
    failed = sum(1 for pod in pod_items if pod.get("status", {}).get("phase") == "Failed")

    cpu_capacity, mem_capacity = _node_capacity_totals(node_items)
    from .k8s_metrics import cluster_resource_usage

    _, _, used_cpu, used_mem = cluster_resource_usage(access, cpu_capacity, mem_capacity)

    return {
        "clusterId": access.cluster_id,
        "healthScore": 100 if failed == 0 else max(65, 100 - failed * 5),
        "workloads": {"deployments": 0, "statefulsets": 0, "daemonsets": 0},
        "resources": {
            "cpu": {"usedCores": used_cpu, "capacityCores": round(cpu_capacity, 2)},
            "memory": {"usedGiB": used_mem, "capacityGiB": round(mem_capacity, 2)},
            "storage": {"usedGiB": 0, "capacityGiB": 0},
        },
        "pods": {"running": running, "pending": pending, "failed": failed},
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


def _resource_counts_by_namespace(access: ClusterAccess) -> Dict[str, Dict[str, int]]:
    counts: Dict[str, Dict[str, int]] = {}
    for resource, key in (
        ("pods", "pods"),
        ("deployments", "deployments"),
        ("services", "services"),
    ):
        try:
            output = _run_for_access(access, ["get", resource, "-A", "-o", "json"])
            for item in json.loads(output).get("items", []):
                namespace = item.get("metadata", {}).get("namespace", "default")
                bucket = counts.setdefault(
                    namespace,
                    {"pods": 0, "deployments": 0, "services": 0},
                )
                bucket[key] += 1
        except K8sCommandError:
            continue
    return counts


def list_namespaces_from_k8s(access: ClusterAccess) -> Dict[str, Any]:
    from .k8s_metrics import aggregate_pod_top_by_namespace

    output = _run_for_access(access, ["get", "namespaces", "-o", "json"])
    data = json.loads(output)
    resource_counts = _resource_counts_by_namespace(access)
    usage_by_namespace = aggregate_pod_top_by_namespace(access)
    items: List[Dict[str, Any]] = []

    for ns in data.get("items", []):
        name = ns.get("metadata", {}).get("name", "unknown")
        counts = resource_counts.get(name, {"pods": 0, "deployments": 0, "services": 0})
        pods_count = counts["pods"]
        ns_usage = usage_by_namespace.get(name, {"cpu": 0.0, "memory_mib": 0.0})
        cpu_cores = round(ns_usage["cpu"], 3)
        memory_gib = round(ns_usage["memory_mib"] / 1024, 2)
        has_metrics = cpu_cores > 0 or memory_gib > 0

        items.append(
            {
                "name": name,
                "pods": pods_count,
                "deployments": counts["deployments"],
                "services": counts["services"],
                "cpuUsageCores": cpu_cores if has_metrics or pods_count == 0 else None,
                "memoryUsageGiB": memory_gib if has_metrics or pods_count == 0 else None,
                "cpuUsage": f"{cpu_cores:.3f} cores" if has_metrics or pods_count == 0 else "-",
                "memoryUsage": f"{memory_gib:.2f} GiB" if has_metrics or pods_count == 0 else "-",
                "alerts": 0,
                "status": "active",
            }
        )
    return {"items": items, "count": len(items)}


def _node_ips_from_items(node_items: List[Dict[str, Any]]) -> List[str]:
    ips: List[str] = []
    for node in node_items:
        addresses = node.get("status", {}).get("addresses") or []
        preferred = None
        for addr_type in ("ExternalIP", "InternalIP"):
            for addr in addresses:
                if addr.get("type") == addr_type and addr.get("address"):
                    preferred = addr["address"]
                    break
            if preferred:
                break
        if preferred and preferred not in ips:
            ips.append(preferred)
    return ips


def _node_port_external_display(spec: Dict[str, Any], node_ips: List[str]) -> str:
    displays: List[str] = []
    for port_spec in spec.get("ports") or []:
        node_port = port_spec.get("nodePort")
        if not node_port:
            continue
        if node_ips:
            for ip in node_ips[:3]:
                displays.append(f"{ip}:{node_port}")
        else:
            displays.append(f"*:{node_port}")
    return ", ".join(displays)


def format_service_external_ip(svc: Dict[str, Any], node_ips: Optional[List[str]] = None) -> str:
    spec = svc.get("spec") or {}
    status = svc.get("status") or {}
    svc_type = spec.get("type", "ClusterIP")
    parts: List[str] = []

    for ip in spec.get("externalIPs") or []:
        val = str(ip).strip()
        if val and val not in parts:
            parts.append(val)

    for ing in (status.get("loadBalancer") or {}).get("ingress") or []:
        for key in ("ip", "hostname"):
            val = ing.get(key)
            if val:
                text = str(val).strip()
                if text and text not in parts:
                    parts.append(text)

    if svc_type == "ExternalName":
        name = spec.get("externalName")
        if name and name not in parts:
            parts.append(str(name))

    if svc_type == "NodePort" and not parts:
        node_port_display = _node_port_external_display(spec, node_ips or [])
        if node_port_display:
            parts.append(node_port_display)

    if svc_type == "LoadBalancer" and not parts:
        return "pending"

    return ", ".join(parts) if parts else "-"


def targets_from_endpoint(endpoint: Dict[str, Any]) -> List[str]:
    targets: List[str] = []
    seen: set[str] = set()
    for subset in endpoint.get("subsets") or []:
        for group_key in ("addresses", "notReadyAddresses"):
            for addr in subset.get(group_key) or []:
                ref = addr.get("targetRef") or {}
                label = None
                if ref.get("kind") == "Pod" and ref.get("name"):
                    label = ref["name"]
                else:
                    label = addr.get("hostname") or addr.get("ip")
                if label:
                    text = str(label)
                    if text not in seen:
                        seen.add(text)
                        targets.append(text)
    return targets


def endpoints_targets_by_name(endpoints_items: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for endpoint in endpoints_items:
        name = (endpoint.get("metadata") or {}).get("name")
        if not name:
            continue
        targets = targets_from_endpoint(endpoint)
        if targets:
            result[name] = targets
    return result


def format_service_target_pods(
    svc: Dict[str, Any],
    endpoints_by_name: Dict[str, List[str]],
) -> str:
    spec = svc.get("spec") or {}
    meta = svc.get("metadata") or {}
    name = meta.get("name") or ""

    if spec.get("type") == "ExternalName":
        return str(spec.get("externalName") or "-")

    targets = endpoints_by_name.get(name, [])
    if targets:
        return ", ".join(targets)
    return "-"


def _get_namespace_items(access: ClusterAccess, resource_kind: str, namespace: str) -> List[Dict[str, Any]]:
    try:
        output = _run_for_access(access, ["get", resource_kind, "-n", namespace, "-o", "json"])
        return json.loads(output).get("items", [])
    except K8sCommandError:
        return []


def _workload_container_image(spec: Dict[str, Any]) -> str:
    containers = (spec.get("template") or {}).get("spec", {}).get("containers") or []
    if not containers:
        job_template = (spec.get("jobTemplate") or {}).get("spec", {}).get("template", {}).get("spec", {})
        containers = job_template.get("containers") or []
    return (containers[0] if containers else {}).get("image", "-")


def _owner_deployment_name(meta: Dict[str, Any]) -> str:
    for ref in meta.get("ownerReferences") or []:
        if ref.get("kind") == "Deployment" and ref.get("name"):
            return ref["name"]
    return ""


NAMESPACE_RESOURCE_LIST_KEYS = (
    "pods",
    "deployments",
    "replicasets",
    "statefulsets",
    "daemonsets",
    "jobs",
    "cronjobs",
    "services",
)


def _namespace_endpoints_by_name(access: ClusterAccess, namespace: str) -> Dict[str, Any]:
    try:
        endpoints_output = _run_for_access(access, ["get", "endpoints", "-n", namespace, "-o", "json"])
        endpoints_items = json.loads(endpoints_output).get("items", [])
    except K8sCommandError:
        endpoints_items = []
    return endpoints_targets_by_name(endpoints_items)


def _node_ips_for_services(access: ClusterAccess, svc_items: List[Dict[str, Any]]) -> List[str]:
    if not any((item.get("spec") or {}).get("type") == "NodePort" for item in svc_items):
        return []
    try:
        nodes_output = _run_for_access(access, ["get", "nodes", "-o", "json"])
        return _node_ips_from_items(json.loads(nodes_output).get("items", []))
    except K8sCommandError:
        return []


def _build_namespace_pods(
    namespace: str,
    pod_items: List[Dict[str, Any]],
    top_by_pod: Dict[Any, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    from .k8s_metrics import (
        cpu_usage_percent,
        format_k8s_age,
        pod_total_cpu_limit_cores,
        pod_total_memory_limit_mib,
    )

    pods: List[Dict[str, Any]] = []
    for pod in pod_items:
        meta = pod.get("metadata", {})
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        container_statuses = status.get("containerStatuses", []) or []
        ready_count = sum(1 for c in container_statuses if c.get("ready"))
        container_names = [c.get("name") for c in (spec.get("containers") or []) if c.get("name")]
        pod_name = meta.get("name")
        top_metrics = top_by_pod.get((namespace, pod_name), {})
        usage_cores = top_metrics.get("cpu")
        usage_mib = top_metrics.get("memory")
        limit_cores = pod_total_cpu_limit_cores(pod)
        limit_mib = pod_total_memory_limit_mib(pod)
        cpu_percent = cpu_usage_percent(usage_cores, limit_cores) if usage_cores is not None else None
        pods.append(
            {
                "name": pod_name,
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "status": status.get("phase", "Unknown"),
                "ready": f"{ready_count}/{len(container_statuses) if container_statuses else 0}",
                "restarts": sum(c.get("restartCount", 0) for c in container_statuses),
                "cpuUsage": f"{usage_cores:.3f}" if usage_cores is not None else "-",
                "memoryUsage": f"{int(usage_mib)}Mi" if usage_mib is not None else "-",
                "memoryUsageMiB": int(usage_mib) if usage_mib is not None else None,
                "cpuLimit": f"{limit_cores:.3f}" if limit_cores > 0 else "-",
                "cpuPercent": cpu_percent,
                "memoryLimit": f"{int(limit_mib)}Mi" if limit_mib > 0 else "-",
                "memoryLimitMiB": int(limit_mib) if limit_mib > 0 else None,
                "node": spec.get("nodeName", "-"),
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "image": (spec.get("containers") or [{}])[0].get("image", "-"),
                "containers": container_names,
                "actions": ["logs", "describe"],
            }
        )
    return pods


def _deployment_config_secret_refs(dep: Dict[str, Any]) -> Tuple[set, set]:
    config_maps: set = set()
    secrets: set = set()
    pod_spec = (dep.get("spec") or {}).get("template", {}).get("spec", {}) or {}
    for container in pod_spec.get("containers") or []:
        for env_from in container.get("envFrom") or []:
            if env_from.get("configMapRef", {}).get("name"):
                config_maps.add(env_from["configMapRef"]["name"])
            if env_from.get("secretRef", {}).get("name"):
                secrets.add(env_from["secretRef"]["name"])
    for volume in pod_spec.get("volumes") or []:
        if volume.get("configMap", {}).get("name"):
            config_maps.add(volume["configMap"]["name"])
        if volume.get("secret", {}).get("secretName"):
            secrets.add(volume["secret"]["secretName"])
    return config_maps, secrets


def _build_namespace_deployments(namespace: str, dep_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    deployments: List[Dict[str, Any]] = []
    for dep in dep_items:
        meta = dep.get("metadata", {})
        spec = dep.get("spec", {})
        stat = dep.get("status", {})
        template_labels = (spec.get("template") or {}).get("metadata", {}).get("labels") or {}
        merged_labels = {**template_labels, **(meta.get("labels") or {})}
        config_map_refs, secret_refs = _deployment_config_secret_refs(dep)
        deployments.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": merged_labels,
                "podLabels": template_labels,
                "configMapRefs": sorted(config_map_refs),
                "secretRefs": sorted(secret_refs),
                "desired": spec.get("replicas", 0),
                "ready": stat.get("readyReplicas", 0),
                "available": stat.get("availableReplicas", 0),
                "image": ((spec.get("template", {}).get("spec", {}).get("containers") or [{}])[0]).get(
                    "image", "-"
                ),
                "updateStatus": "healthy" if stat.get("unavailableReplicas", 0) == 0 else "warning",
                "upToDate": stat.get("updatedReplicas", 0),
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "actions": ["pods", "rollout", "yaml"],
            }
        )
    return deployments


def _build_namespace_services(
    namespace: str,
    svc_items: List[Dict[str, Any]],
    endpoints_by_name: Dict[str, Any],
    node_ips: List[str],
) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    services: List[Dict[str, Any]] = []
    for svc in svc_items:
        meta = svc.get("metadata", {})
        spec = svc.get("spec", {})
        services.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "selector": spec.get("selector") or {},
                "type": spec.get("type", "ClusterIP"),
                "clusterIP": spec.get("clusterIP", "-"),
                "externalIP": format_service_external_ip(svc, node_ips),
                "ports": ",".join(str(p.get("port")) for p in spec.get("ports", [])),
                "targetPods": format_service_target_pods(svc, endpoints_by_name),
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "actions": ["describe"],
            }
        )
    return services


def _build_namespace_configmaps(namespace: str, cm_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    items: List[Dict[str, Any]] = []
    for cm in cm_items:
        meta = cm.get("metadata", {})
        data = cm.get("data") or {}
        items.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "keys": len(data),
                "age": format_k8s_age(meta.get("creationTimestamp")),
            }
        )
    return items


def _build_namespace_secrets(namespace: str, secret_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    items: List[Dict[str, Any]] = []
    for secret in secret_items:
        meta = secret.get("metadata", {})
        items.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "type": secret.get("type") or "Opaque",
                "age": format_k8s_age(meta.get("creationTimestamp")),
            }
        )
    return items


def _build_namespace_ingress(namespace: str, ing_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    items: List[Dict[str, Any]] = []
    for ing in ing_items:
        meta = ing.get("metadata", {})
        spec = ing.get("spec") or {}
        rules = spec.get("rules") or []
        host = rules[0].get("host", "") if rules else ""
        path = "/"
        backend_service = ""
        if rules:
            paths = (rules[0].get("http") or {}).get("paths") or []
            if paths:
                path = paths[0].get("path") or "/"
                backend = paths[0].get("backend") or {}
                backend_service = (backend.get("service") or {}).get("name") or ""
        tls_entries = spec.get("tls") or []
        items.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "host": host,
                "path": path,
                "backendService": backend_service,
                "tlsEnabled": bool(tls_entries),
                "age": format_k8s_age(meta.get("creationTimestamp")),
            }
        )
    return items


def _build_namespace_replicasets(namespace: str, rs_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    replicasets: List[Dict[str, Any]] = []
    for rs in rs_items:
        meta = rs.get("metadata", {})
        spec = rs.get("spec", {})
        stat = rs.get("status", {})
        owner = _owner_deployment_name(meta) or "-"
        replicasets.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "owner": owner,
                "desired": spec.get("replicas", 0),
                "ready": stat.get("readyReplicas", 0),
                "image": _workload_container_image(spec),
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "actions": ["pods", "describe", "yaml"],
            }
        )
    return replicasets


def _build_namespace_statefulsets(namespace: str, sts_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    statefulsets: List[Dict[str, Any]] = []
    for sts in sts_items:
        meta = sts.get("metadata", {})
        spec = sts.get("spec", {})
        stat = sts.get("status", {})
        statefulsets.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "desired": spec.get("replicas", 0),
                "ready": stat.get("readyReplicas", 0),
                "available": stat.get("availableReplicas", 0),
                "image": _workload_container_image(spec),
                "updateStatus": "healthy" if stat.get("unavailableReplicas", 0) == 0 else "warning",
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "actions": ["pods", "describe", "yaml"],
            }
        )
    return statefulsets


def _build_namespace_daemonsets(namespace: str, ds_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    daemonsets: List[Dict[str, Any]] = []
    for ds in ds_items:
        meta = ds.get("metadata", {})
        stat = ds.get("status", {})
        spec = ds.get("spec", {})
        daemonsets.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "desired": stat.get("desiredNumberScheduled", 0),
                "ready": stat.get("numberReady", 0),
                "available": stat.get("numberAvailable", 0),
                "image": _workload_container_image(spec),
                "updateStatus": "healthy" if stat.get("numberUnavailable", 0) == 0 else "warning",
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "actions": ["pods", "describe", "yaml"],
            }
        )
    return daemonsets


def _build_namespace_jobs(namespace: str, job_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    jobs: List[Dict[str, Any]] = []
    for job in job_items:
        meta = job.get("metadata", {})
        spec = job.get("spec", {})
        stat = job.get("status", {})
        completions = spec.get("completions") or 1
        succeeded = stat.get("succeeded") or 0
        if stat.get("succeeded") is not None and succeeded >= completions:
            status = "Complete"
        elif stat.get("failed"):
            status = "Failed"
        elif stat.get("active"):
            status = "Running"
        else:
            status = "Pending"
        jobs.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "completions": f"{succeeded}/{completions}",
                "status": status,
                "image": _workload_container_image(spec),
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "actions": ["pods", "describe", "yaml"],
            }
        )
    return jobs


def _build_namespace_cronjobs(namespace: str, cj_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .k8s_metrics import format_k8s_age

    cronjobs: List[Dict[str, Any]] = []
    for cj in cj_items:
        meta = cj.get("metadata", {})
        spec = cj.get("spec", {})
        stat = cj.get("status", {})
        last_schedule = stat.get("lastScheduleTime")
        cronjobs.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "labels": meta.get("labels") or {},
                "schedule": spec.get("schedule", "-"),
                "suspend": bool(spec.get("suspend")),
                "active": len(stat.get("active") or []),
                "lastSchedule": format_k8s_age(last_schedule) if last_schedule else "-",
                "age": format_k8s_age(meta.get("creationTimestamp")),
                "actions": ["describe", "yaml"],
            }
        )
    return cronjobs


def namespace_resources_from_k8s(access: ClusterAccess, namespace: str) -> Dict[str, Any]:
    pod_items = _get_namespace_items(access, "pods", namespace)
    dep_items = _get_namespace_items(access, "deployments", namespace)
    svc_items = _get_namespace_items(access, "services", namespace)
    rs_items = _get_namespace_items(access, "replicasets", namespace)
    sts_items = _get_namespace_items(access, "statefulsets", namespace)
    ds_items = _get_namespace_items(access, "daemonsets", namespace)
    job_items = _get_namespace_items(access, "jobs", namespace)
    cj_items = _get_namespace_items(access, "cronjobs", namespace)
    cm_items = _get_namespace_items(access, "configmaps", namespace)
    secret_items = _get_namespace_items(access, "secrets", namespace)
    ing_items = _get_namespace_items(access, "ingress", namespace)
    endpoints_by_name = _namespace_endpoints_by_name(access, namespace)

    from .k8s_metrics import fetch_pod_top_metrics

    top_by_pod = fetch_pod_top_metrics(access)
    node_ips = _node_ips_for_services(access, svc_items)

    return {
        "namespace": namespace,
        "pods": _build_namespace_pods(namespace, pod_items, top_by_pod),
        "deployments": _build_namespace_deployments(namespace, dep_items),
        "replicasets": _build_namespace_replicasets(namespace, rs_items),
        "statefulsets": _build_namespace_statefulsets(namespace, sts_items),
        "daemonsets": _build_namespace_daemonsets(namespace, ds_items),
        "jobs": _build_namespace_jobs(namespace, job_items),
        "cronjobs": _build_namespace_cronjobs(namespace, cj_items),
        "services": _build_namespace_services(namespace, svc_items, endpoints_by_name, node_ips),
        "configMaps": _build_namespace_configmaps(namespace, cm_items),
        "secrets": _build_namespace_secrets(namespace, secret_items),
        "ingress": _build_namespace_ingress(namespace, ing_items),
    }


def namespace_resource_list_from_k8s(
    access: ClusterAccess, namespace: str, list_key: str
) -> Dict[str, Any]:
    if list_key not in NAMESPACE_RESOURCE_LIST_KEYS:
        raise ValueError(f"Unsupported resource type: {list_key}")

    if list_key == "pods":
        from .k8s_metrics import fetch_pod_top_metrics

        pod_items = _get_namespace_items(access, "pods", namespace)
        top_by_pod = fetch_pod_top_metrics(access)
        items = _build_namespace_pods(namespace, pod_items, top_by_pod)
    elif list_key == "services":
        svc_items = _get_namespace_items(access, "services", namespace)
        endpoints_by_name = _namespace_endpoints_by_name(access, namespace)
        node_ips = _node_ips_for_services(access, svc_items)
        items = _build_namespace_services(namespace, svc_items, endpoints_by_name, node_ips)
    elif list_key == "deployments":
        dep_items = _get_namespace_items(access, "deployments", namespace)
        items = _build_namespace_deployments(namespace, dep_items)
    elif list_key == "replicasets":
        rs_items = _get_namespace_items(access, "replicasets", namespace)
        items = _build_namespace_replicasets(namespace, rs_items)
    elif list_key == "statefulsets":
        sts_items = _get_namespace_items(access, "statefulsets", namespace)
        items = _build_namespace_statefulsets(namespace, sts_items)
    elif list_key == "daemonsets":
        ds_items = _get_namespace_items(access, "daemonsets", namespace)
        items = _build_namespace_daemonsets(namespace, ds_items)
    elif list_key == "jobs":
        job_items = _get_namespace_items(access, "jobs", namespace)
        items = _build_namespace_jobs(namespace, job_items)
    else:
        cj_items = _get_namespace_items(access, "cronjobs", namespace)
        items = _build_namespace_cronjobs(namespace, cj_items)

    return {"namespace": namespace, list_key: items}


def _event_timestamp_value(event: Dict[str, Any]) -> float:
    for key in ("lastTimestamp", "eventTime", "firstTimestamp"):
        val = event.get(key)
        if val:
            try:
                dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                return dt.timestamp()
            except ValueError:
                continue
    created = event.get("metadata", {}).get("creationTimestamp")
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            pass
    return 0.0


def _normalize_k8s_event(event: Dict[str, Any]) -> Dict[str, Any]:
    from .k8s_metrics import format_k8s_age

    involved = event.get("involvedObject") or {}
    source = event.get("source") or {}
    meta = event.get("metadata") or {}
    first_ts = event.get("firstTimestamp") or meta.get("creationTimestamp")
    last_ts = event.get("lastTimestamp") or event.get("eventTime") or first_ts
    age_ref = last_ts or first_ts or meta.get("creationTimestamp")
    return {
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": event.get("message"),
        "involvedKind": involved.get("kind"),
        "involvedName": involved.get("name"),
        "count": event.get("count", 1),
        "firstTimestamp": first_ts,
        "lastTimestamp": last_ts,
        "age": format_k8s_age(age_ref),
        "source": {"component": source.get("component")},
    }


def namespace_events_from_k8s(
    access: ClusterAccess,
    namespace: str,
    *,
    involved_kind: Optional[str] = None,
    involved_name: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    output = _run_for_access(access, ["get", "events", "-n", namespace, "-o", "json"])
    raw_items = json.loads(output).get("items", [])

    if involved_kind:
        kind_filter = involved_kind.strip()
        raw_items = [
            item
            for item in raw_items
            if (item.get("involvedObject") or {}).get("kind") == kind_filter
        ]
    if involved_name:
        name_filter = involved_name.strip()
        raw_items = [
            item
            for item in raw_items
            if (item.get("involvedObject") or {}).get("name") == name_filter
        ]

    raw_items.sort(key=_event_timestamp_value, reverse=True)
    if limit is not None and limit > 0:
        raw_items = raw_items[:limit]

    items = [_normalize_k8s_event(event) for event in raw_items]
    return {
        "clusterId": access.cluster_id,
        "namespace": namespace,
        "items": items,
        "count": len(items),
    }


def list_namespace_pods_for_logs(access: ClusterAccess, namespace: str) -> Dict[str, Any]:
    """Lightweight pod list for the logs page (no metrics)."""
    pod_items = _get_namespace_items(access, "pods", namespace)
    items: List[Dict[str, Any]] = []
    for pod in pod_items:
        meta = pod.get("metadata", {})
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        container_names = [c.get("name") for c in (spec.get("containers") or []) if c.get("name")]
        items.append(
            {
                "name": meta.get("name"),
                "namespace": namespace,
                "status": status.get("phase", "Unknown"),
                "containers": container_names,
            }
        )
    return {
        "clusterId": access.cluster_id,
        "namespace": namespace,
        "items": items,
        "count": len(items),
    }


def list_pod_containers_from_k8s(
    access: ClusterAccess, namespace: str, pod_name: str
) -> Dict[str, Any]:
    output = _run_for_access(
        access,
        ["get", "pod", pod_name, "-n", namespace, "-o", "json"],
    )
    pod = json.loads(output)
    spec = pod.get("spec") or {}
    containers = [
        {"name": c.get("name"), "image": c.get("image", "-")}
        for c in (spec.get("containers") or [])
        if c.get("name")
    ]
    return {
        "clusterId": access.cluster_id,
        "namespace": namespace,
        "pod": pod_name,
        "items": containers,
        "count": len(containers),
    }


def pod_logs_from_k8s(
    *,
    access: ClusterAccess,
    namespace: str,
    pod: str,
    container: Optional[str],
    live: bool,
    previous: bool,
    since_seconds: Optional[int] = None,
    since_time: Optional[datetime] = None,
    until_time: Optional[datetime] = None,
    tail_lines: Optional[int] = None,
    timestamps: bool = True,
) -> Dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    from .log_time_filters import (
        advance_log_cursor,
        filter_log_lines_after,
        filter_log_lines_until,
        format_rfc3339_z,
    )

    incremental_tail = since_time is not None and since_seconds is None
    effective_since_time = since_time
    if incremental_tail and since_time is not None:
        effective_since_time = advance_log_cursor(since_time)
    elif effective_since_time is None and since_seconds is not None:
        effective_since_time = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)

    if tail_lines is not None:
        tail = tail_lines
    elif live and incremental_tail:
        tail = 50
    elif live:
        tail = 500
    else:
        tail = 200
    args = ["logs", pod, "-n", namespace, f"--tail={tail}"]
    if timestamps:
        args.append("--timestamps")
    if container:
        args += ["-c", container]
    if previous:
        args += ["--previous"]
    if effective_since_time is not None:
        args += ["--since-time", format_rfc3339_z(effective_since_time)]

    from .log_noise import filter_live_log_noise

    output = _run_for_access(access, args)
    lines = [line for line in output.splitlines() if line.strip()]
    if until_time is not None:
        lines = filter_log_lines_until(lines, until_time)
    if incremental_tail and since_time is not None:
        lines = filter_log_lines_after(lines, since_time)
    if live:
        lines = filter_live_log_noise(lines)
    else:
        from .log_noise import filter_health_probe_log_lines

        lines = filter_health_probe_log_lines(lines)

    query: Dict[str, Any] = {
        "cluster": access.cluster_id,
        "namespace": namespace,
        "pod": pod,
        "container": container or "",
        "live": live,
        "previous": previous,
        "timestamps": timestamps,
    }
    if tail_lines is not None:
        query["tailLines"] = tail_lines
    if since_seconds is not None:
        query["sinceSeconds"] = since_seconds
    if since_time is not None:
        query["sinceTime"] = format_rfc3339_z(since_time)
    if until_time is not None:
        query["untilTime"] = format_rfc3339_z(until_time)

    return {
        "query": query,
        "stream": "live" if live else "snapshot",
        "lines": lines,
    }


def _parse_k8s_version(version: str) -> tuple:
    """Parse v1.28.3 -> (1, 28, 3) for comparison. Delegates to upgrade_provider."""
    from .upgrade_provider import parse_k8s_version

    return parse_k8s_version(version)


def run_upgrade_precheck_k8s(access: ClusterAccess, target_version: str) -> Dict[str, Any]:
    from .upgrade_provider import run_extended_prechecks

    return run_extended_prechecks(access, target_version, _run_for_access)


def run_upgrade_start_k8s(access: ClusterAccess, target_version: str) -> Dict[str, Any]:
    from .upgrade_provider import run_upgrade_workflow

    return run_upgrade_workflow(access, target_version, _run_for_access)


def list_alerts_for_access(access: ClusterAccess, cluster_id: Optional[str] = None) -> Dict[str, Any]:
    """CPU-threshold alerts for a single cluster without listing every kube context."""
    from .k8s_metrics import (
        CPU_ALERT_THRESHOLD_PERCENT,
        cpu_usage_percent,
        fetch_pod_cpu_usage_cores,
        pod_total_cpu_limit_cores,
    )

    current_cluster_id = cluster_id or access.cluster_id
    generated_at = datetime.now(timezone.utc).isoformat()
    items: List[Dict[str, Any]] = []
    metrics_unavailable = False

    usage_by_pod = fetch_pod_cpu_usage_cores(access)
    if not usage_by_pod:
        metrics_unavailable = True

    pods_output = _run_for_access(access, ["get", "pods", "--all-namespaces", "-o", "json"])
    pod_items = json.loads(pods_output).get("items", [])
    for pod in pod_items:
        metadata = pod.get("metadata", {})
        namespace = metadata.get("namespace", "default")
        pod_name = metadata.get("name", "unknown")
        usage_cores = usage_by_pod.get((namespace, pod_name))
        limit_cores = pod_total_cpu_limit_cores(pod)

        if usage_cores is None or limit_cores <= 0:
            continue

        percent = cpu_usage_percent(usage_cores, limit_cores)
        if percent <= CPU_ALERT_THRESHOLD_PERCENT:
            continue

        items.append(
            {
                "id": f"{current_cluster_id}:{namespace}:{pod_name}:cpu",
                "severity": "critical" if percent >= 95 else "warning",
                "clusterId": current_cluster_id,
                "namespace": namespace,
                "pod": pod_name,
                "title": f"High CPU on pod {pod_name}",
                "description": (
                    f"CPU usage {percent}% of limit "
                    f"({usage_cores:.3f}/{limit_cores:.3f} cores) in namespace {namespace}"
                ),
                "cpuPercent": percent,
                "cpuUsageCores": usage_cores,
                "cpuLimitCores": limit_cores,
                "firedAt": generated_at,
                "status": "firing",
            }
        )

    metadata = {
        "mode": "real",
        "source": "cpu_usage_vs_limit",
        "thresholdPercent": CPU_ALERT_THRESHOLD_PERCENT,
        "formula": "(cpu_usage / cpu_limit) * 100",
        "generatedAt": generated_at,
        "hasLiveAlertsSource": not metrics_unavailable,
        "clusterId": current_cluster_id,
    }
    if metrics_unavailable:
        metadata["reason"] = "metrics_server_unavailable"
        metadata["detail"] = "Install metrics-server or ensure kubectl top pods works."

    return {"items": items, "count": len(items), "metadata": metadata}


def list_alerts_from_k8s(cluster_id: Optional[str] = None) -> Dict[str, Any]:
    if cluster_id:
        access = resolve_cluster_access(cluster_id)
        if not access:
            return {
                "items": [],
                "count": 0,
                "metadata": {
                    "mode": "real",
                    "source": "none",
                    "clusterId": cluster_id,
                    "hasLiveAlertsSource": True,
                    "reason": "cluster_not_found",
                },
            }
        return list_alerts_for_access(access, cluster_id)

    clusters = list_clusters_from_k8s().get("items", [])
    items: List[Dict[str, Any]] = []
    generated_at = datetime.now(timezone.utc).isoformat()
    metrics_unavailable = False

    for cluster in clusters:
        current_cluster_id = cluster.get("id")
        if not current_cluster_id:
            continue
        access = resolve_cluster_access(current_cluster_id)
        if not access:
            continue

        result = list_alerts_for_access(access, current_cluster_id)
        items.extend(result.get("items", []))
        if not result.get("metadata", {}).get("hasLiveAlertsSource", True):
            metrics_unavailable = True

    metadata = {
        "mode": "real",
        "source": "cpu_usage_vs_limit",
        "thresholdPercent": float(os.getenv("ALERT_CPU_THRESHOLD_PERCENT", "80")),
        "formula": "(cpu_usage / cpu_limit) * 100",
        "generatedAt": generated_at,
        "hasLiveAlertsSource": not metrics_unavailable,
        "clusterId": cluster_id,
    }
    if metrics_unavailable:
        metadata["reason"] = "metrics_server_unavailable"
        metadata["detail"] = "Install metrics-server or ensure kubectl top pods works."

    return {"items": items, "count": len(items), "metadata": metadata}

