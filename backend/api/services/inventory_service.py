"""Application inventory discovery, grouping, health, and RBAC-aware responses."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from ..access_engine import (
    can_access_namespace,
    can_access_resource,
    can_view_alert,
    can_view_logs,
    filter_alerts_for_user,
    filter_clusters_for_user,
    filter_namespaces_for_user,
    is_admin,
    user_has_permission,
)
from ..k8s_metrics import fetch_pod_top_metrics, format_k8s_age, metrics_server_available
from ..k8s_provider import (
    K8sCommandError,
    list_clusters_from_k8s,
    namespace_resources_from_k8s,
    resolve_cluster_access,
    should_use_real_k8s,
)
from ..mock_data import ALERTS, CLUSTERS, HELM_RELEASES, HELM_RELEASE_DETAILS, INVENTORY_DETAIL_EXTRAS, NAMESPACE_RESOURCES, NAMESPACES
from ..models import User

WORKLOAD_TYPES = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")
RESOURCE_TYPE_BY_WORKLOAD = {
    "Deployment": "deployment",
    "StatefulSet": "deployment",
    "DaemonSet": "deployment",
    "Job": "deployment",
    "CronJob": "deployment",
}


def make_inventory_id(cluster_id: str, namespace: str, name: str) -> str:
    return quote(f"{cluster_id}|{namespace}|{name}", safe="")


def parse_inventory_id(inventory_id: str) -> Optional[Tuple[str, str, str]]:
    try:
        raw = unquote(inventory_id)
    except Exception:
        return None
    parts = raw.split("|", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def _inventory_ids_equal(left: str, right: str) -> bool:
    try:
        return unquote(left or "") == unquote(right or "")
    except Exception:
        return left == right


def resolve_app_name(labels: Dict[str, str], fallback: str) -> str:
    labels = labels or {}
    if labels.get("app.kubernetes.io/name"):
        return labels["app.kubernetes.io/name"]
    if labels.get("app"):
        return labels["app"]
    if labels.get("app.kubernetes.io/instance"):
        return labels["app.kubernetes.io/instance"]
    return fallback


def _belongs_to_app(
    obj: Dict[str, Any],
    app_name: str,
    workload_names: Optional[List[str]] = None,
    name_key: str = "name",
) -> bool:
    name = obj.get(name_key) or ""
    labels = obj.get("labels") or {}
    if resolve_app_name(labels, name) == app_name:
        return True
    if name == app_name or name.startswith(f"{app_name}-"):
        return True
    for workload_name in workload_names or []:
        if workload_name and (name == workload_name or name.startswith(f"{workload_name}-")):
            return True
    return False


def _selector_matches_pod_labels(selector: Dict[str, Any], pod_labels: Dict[str, Any]) -> bool:
    if not selector:
        return False
    return all(pod_labels.get(key) == value for key, value in selector.items())


def _service_belongs_to_app(
    service: Dict[str, Any],
    app_name: str,
    workload_names: Optional[List[str]],
    pod_label_sets: List[Dict[str, Any]],
    linked_service_names: set,
) -> bool:
    if _belongs_to_app(service, app_name, workload_names):
        return True
    if service.get("name") in linked_service_names:
        return True
    selector = service.get("selector") or {}
    return any(_selector_matches_pod_labels(selector, labels) for labels in pod_label_sets if labels)


def _filter_resources_for_app(
    resources: Dict[str, Any],
    app_name: str,
    workload_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    filtered = dict(resources)
    filtered["deployments"] = [
        d for d in resources.get("deployments") or [] if _belongs_to_app(d, app_name, workload_names)
    ]
    filtered["statefulsets"] = [
        s for s in resources.get("statefulsets") or [] if _belongs_to_app(s, app_name, workload_names)
    ]
    filtered["daemonsets"] = [
        d for d in resources.get("daemonsets") or [] if _belongs_to_app(d, app_name, workload_names)
    ]
    workloads = filtered["deployments"] + filtered["statefulsets"] + filtered["daemonsets"]

    pod_label_sets = [
        labels
        for workload in workloads
        for labels in [workload.get("podLabels") or workload.get("labels") or {}]
        if labels
    ]
    config_map_refs = {name for workload in workloads for name in workload.get("configMapRefs") or []}
    secret_refs = {name for workload in workloads for name in workload.get("secretRefs") or []}

    service_names = {s.get("name") for s in resources.get("services") or [] if s.get("name")}
    linked_service_names: set = set()
    for ing in resources.get("ingress") or []:
        backend = ing.get("backendService") or ""
        if backend in service_names:
            linked_service_names.add(backend)

    filtered["services"] = [
        s
        for s in resources.get("services") or []
        if _service_belongs_to_app(s, app_name, workload_names, pod_label_sets, linked_service_names)
    ]
    linked_service_names.update(s.get("name") for s in filtered["services"] if s.get("name"))

    filtered["pods"] = [
        p for p in resources.get("pods") or [] if _belongs_to_app(p, app_name, workload_names)
    ]
    filtered["configMaps"] = [
        cm
        for cm in resources.get("configMaps") or []
        if cm.get("name") in config_map_refs or _belongs_to_app(cm, app_name, workload_names)
    ]
    filtered["secrets"] = [
        sec
        for sec in resources.get("secrets") or []
        if sec.get("name") in secret_refs or _belongs_to_app(sec, app_name, workload_names)
    ]
    filtered["ingress"] = [
        ing
        for ing in resources.get("ingress") or []
        if (ing.get("backendService") in linked_service_names)
        or _belongs_to_app(ing, app_name, workload_names)
    ]
    return filtered


def _format_inventory_cpu_usage(cpu_cores: float) -> str:
    if cpu_cores <= 0:
        return "-"
    if cpu_cores < 1:
        return f"{int(round(cpu_cores * 1000))}m"
    return f"{cpu_cores:.3f} cores"


def _format_inventory_memory_usage(memory_mib: float) -> str:
    if memory_mib <= 0:
        return "-"
    if memory_mib >= 1024:
        return f"{memory_mib / 1024:.2f} GiB"
    return f"{int(round(memory_mib))} MiB"


def _build_app_usage_index(
    access: Any,
    top_by_pod: Dict[Tuple[str, str], Dict[str, float]],
) -> Dict[Tuple[str, str], Dict[str, float]]:
    """Sum kubectl top pod metrics by (namespace, resolved app name)."""
    from ..k8s_provider import _run_for_access

    index: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(lambda: {"cpu": 0.0, "memory_mib": 0.0})
    if not top_by_pod:
        return index
    try:
        output = _run_for_access(access, ["get", "pods", "-A", "-o", "json"])
    except K8sCommandError:
        return index
    for pod in json.loads(output).get("items", []):
        meta = pod.get("metadata") or {}
        namespace = meta.get("namespace") or "default"
        pod_name = meta.get("name") or ""
        labels = meta.get("labels") or {}
        app_name = resolve_app_name(labels, pod_name)
        metrics = top_by_pod.get((namespace, pod_name))
        if not metrics:
            continue
        key = (namespace, app_name)
        index[key]["cpu"] += float(metrics.get("cpu") or 0.0)
        index[key]["memory_mib"] += float(metrics.get("memory") or 0.0)
    return index


def _usage_fields_for_app(
    namespace: str,
    app_name: str,
    metrics_available: bool,
    app_usage_index: Dict[Tuple[str, str], Dict[str, float]],
) -> Tuple[str, str, float, float]:
    if not metrics_available:
        return "Metrics unavailable", "Metrics unavailable", 0.0, 0.0
    usage = app_usage_index.get((namespace, app_name), {"cpu": 0.0, "memory_mib": 0.0})
    cpu_cores = float(usage.get("cpu") or 0.0)
    memory_mib = float(usage.get("memory_mib") or 0.0)
    return (
        _format_inventory_cpu_usage(cpu_cores),
        _format_inventory_memory_usage(memory_mib),
        cpu_cores,
        memory_mib,
    )


def extract_version_tag(image: str) -> str:
    if not image or image == "-":
        return "-"
    if "@" in image:
        image = image.split("@", 1)[0]
    if ":" in image.rsplit("/", 1)[-1]:
        return image.rsplit(":", 1)[-1]
    return "latest"


def compute_status(
    *,
    desired: int,
    ready: int,
    has_failed_pods: bool = False,
    has_crashloop: bool = False,
) -> str:
    if has_crashloop or has_failed_pods:
        return "Critical"
    if desired <= 0:
        return "Unknown"
    if ready >= desired:
        return "Healthy"
    if ready > 0:
        return "Warning"
    return "Critical"


def _cluster_display_name(cluster_id: str) -> str:
    for cluster in CLUSTERS:
        if cluster.get("id") == cluster_id:
            return cluster.get("name") or cluster_id
    return cluster_id


def _workload_resource_type(workload_type: str) -> str:
    return RESOURCE_TYPE_BY_WORKLOAD.get(workload_type, "deployment")


def can_access_inventory_app(user: Optional[User], item: Dict[str, Any]) -> bool:
    if not user:
        return True
    if is_admin(user):
        return True
    cluster_id = item.get("cluster") or item.get("clusterId")
    namespace = item.get("namespace")
    if not cluster_id or not namespace:
        return False
    if not can_access_namespace(user, cluster_id, namespace):
        return False
    workload_names = item.get("workloadNames") or [item.get("name")]
    workload_type = item.get("workloadType") or "Deployment"
    resource_type = _workload_resource_type(workload_type)
    for name in workload_names:
        if name and can_access_resource(user, cluster_id, namespace, resource_type, name):
            return True
    return False


def filter_inventory_for_user(user: Optional[User], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not user or is_admin(user):
        return items
    return [item for item in items if can_access_inventory_app(user, item)]


def _apply_list_filters(items: List[Dict[str, Any]], filters: Dict[str, str]) -> List[Dict[str, Any]]:
    cluster = (filters.get("cluster") or "").strip().lower()
    namespace = (filters.get("namespace") or "").strip().lower()
    name = (filters.get("name") or filters.get("applicationName") or "").strip().lower()
    status = (filters.get("status") or "").strip().lower()
    workload_type = (filters.get("workloadType") or "").strip().lower()
    image_tag = (filters.get("imageTag") or "").strip().lower()
    search = (filters.get("search") or "").strip().lower()

    result = items
    if cluster:
        result = [i for i in result if (i.get("cluster") or "").lower() == cluster or (i.get("clusterId") or "").lower() == cluster]
    if namespace:
        result = [i for i in result if (i.get("namespace") or "").lower() == namespace]
    if name:
        result = [i for i in result if name in (i.get("name") or "").lower()]
    if status:
        result = [i for i in result if (i.get("status") or "").lower() == status]
    if workload_type:
        result = [i for i in result if (i.get("workloadType") or "").lower() == workload_type]
    if image_tag:
        result = [i for i in result if image_tag in (i.get("versionTag") or extract_version_tag(i.get("image") or "")).lower()]
    if search:
        hay_fields = (
            "name",
            "cluster",
            "namespace",
            "image",
            "service",
            "workloadType",
        )

        def matches(item: Dict[str, Any]) -> bool:
            blob = " ".join(str(item.get(k) or "") for k in hay_fields).lower()
            ports = item.get("ports") or []
            if ports:
                blob += " " + " ".join(str(p) for p in ports)
            return search in blob

        result = [i for i in result if matches(i)]
    return result


def _parse_replica_counts(ready_field: Any, desired_field: Any = None) -> Tuple[int, int]:
    if isinstance(ready_field, dict):
        return int(ready_field.get("ready") or 0), int(ready_field.get("desired") or 0)
    if isinstance(ready_field, str) and "/" in ready_field:
        parts = ready_field.split("/")
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    ready = int(ready_field or 0) if ready_field is not None else 0
    desired = int(desired_field or ready) if desired_field is not None else ready
    return ready, desired


def _build_row_from_workload(
    *,
    cluster_id: str,
    namespace: str,
    app_name: str,
    workload_type: str,
    workload_name: str,
    desired: int,
    ready: int,
    image: str,
    service_name: str,
    ports: List[int],
    cpu_usage: str,
    memory_usage: str,
    last_updated: str,
    status: str,
    workload_names: List[str],
) -> Dict[str, Any]:
    version_tag = extract_version_tag(image)
    return {
        "id": make_inventory_id(cluster_id, namespace, app_name),
        "name": app_name,
        "cluster": cluster_id,
        "clusterId": cluster_id,
        "clusterName": _cluster_display_name(cluster_id),
        "namespace": namespace,
        "workloadType": workload_type,
        "workloadNames": workload_names,
        "status": status,
        "replicas": desired,
        "readyReplicas": ready,
        "image": image,
        "versionTag": version_tag,
        "service": service_name or "-",
        "ports": ports,
        "cpuUsage": cpu_usage,
        "memoryUsage": memory_usage,
        "lastUpdated": last_updated,
        "ownerTeam": "Unassigned",
        "environment": "Not set",
        "criticality": "Not set",
        "documentationUrl": None,
        "contactEmail": "Not set",
        "tags": [],
        "source": "Discovered",
        "catalogEntryId": None,
    }


def _merge_workload_into_group(groups: Dict[Tuple[str, str, str], Dict[str, Any]], row: Dict[str, Any]) -> None:
    key = (row["cluster"], row["namespace"], row["name"])
    existing = groups.get(key)
    if not existing:
        groups[key] = row
        return
    existing["workloadNames"] = sorted(
        set(existing.get("workloadNames") or []) | set(row.get("workloadNames") or [])
    )
    existing["replicas"] = (existing.get("replicas") or 0) + (row.get("replicas") or 0)
    existing["readyReplicas"] = (existing.get("readyReplicas") or 0) + (row.get("readyReplicas") or 0)
    existing["status"] = compute_status(
        desired=int(existing.get("replicas") or 0),
        ready=int(existing.get("readyReplicas") or 0),
        has_failed_pods=row.get("status") == "Critical" or existing.get("status") == "Critical",
    )
    existing["_cpuCores"] = float(existing.get("_cpuCores") or 0.0) + float(row.get("_cpuCores") or 0.0)
    existing["_memoryMib"] = float(existing.get("_memoryMib") or 0.0) + float(row.get("_memoryMib") or 0.0)
    existing["cpuUsage"] = _format_inventory_cpu_usage(existing["_cpuCores"])
    existing["memoryUsage"] = _format_inventory_memory_usage(existing["_memoryMib"])


def _mock_inventory_items() -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for cluster_id, ns_map in NAMESPACE_RESOURCES.items():
        for namespace, resources in ns_map.items():
            services_by_name = {s["name"]: s for s in resources.get("services") or []}
            for dep in resources.get("deployments") or []:
                dep_name = dep.get("name") or "unknown"
                labels = dep.get("labels") or {}
                app_name = resolve_app_name(labels, dep_name)
                ready, desired = _parse_replica_counts(
                    dep.get("ready") or dep.get("replicas"),
                    dep.get("desired"),
                )
                raw_status = str(dep.get("status") or "").lower()
                has_failed = raw_status in {"failed", "critical", "error"}
                status = compute_status(desired=desired, ready=ready, has_failed_pods=has_failed)
                svc = services_by_name.get(dep_name) or services_by_name.get(app_name) or {}
                ports_raw = svc.get("ports") or []
                ports = [int(p) for p in ports_raw if str(p).isdigit()]
                cpu = dep.get("cpuUsageMillicores")
                mem = dep.get("memoryUsageMiB")
                cpu_usage = f"{cpu}m" if cpu is not None else "-"
                memory_usage = f"{mem}Mi" if mem is not None else "-"
                row = _build_row_from_workload(
                    cluster_id=cluster_id,
                    namespace=namespace,
                    app_name=app_name,
                    workload_type="Deployment",
                    workload_name=dep_name,
                    desired=desired,
                    ready=ready,
                    image=dep.get("image") or "-",
                    service_name=svc.get("name") or dep_name,
                    ports=ports,
                    cpu_usage=cpu_usage,
                    memory_usage=memory_usage,
                    last_updated=now,
                    status=status,
                    workload_names=[dep_name],
                )
                _merge_workload_into_group(groups, row)

    extra_apps = [
        {
            "clusterId": "prod-us-east",
            "namespace": "checkout",
            "name": "checkout-api",
            "workloadType": "Deployment",
            "workloadNames": ["checkout-api"],
            "replicas": 4,
            "readyReplicas": 4,
            "image": "ghcr.io/mock/checkout:v3.2.0",
            "service": "checkout-service",
            "ports": [8080],
            "status": "Healthy",
            "cpuUsage": "1.2 cores",
            "memoryUsage": "512 MiB",
        },
        {
            "clusterId": "prod-us-east",
            "namespace": "payments",
            "name": "redis",
            "workloadType": "StatefulSet",
            "workloadNames": ["redis"],
            "replicas": 3,
            "readyReplicas": 2,
            "image": "redis:7.2",
            "service": "redis",
            "ports": [6379],
            "status": "Warning",
            "cpuUsage": "0.4 cores",
            "memoryUsage": "1.2 GiB",
        },
    ]
    for extra in extra_apps:
        row = _build_row_from_workload(
            cluster_id=extra["clusterId"],
            namespace=extra["namespace"],
            app_name=extra["name"],
            workload_type=extra["workloadType"],
            workload_name=extra["workloadNames"][0],
            desired=extra["replicas"],
            ready=extra["readyReplicas"],
            image=extra["image"],
            service_name=extra["service"],
            ports=extra["ports"],
            cpu_usage=extra["cpuUsage"],
            memory_usage=extra["memoryUsage"],
            last_updated=now,
            status=extra["status"],
            workload_names=extra["workloadNames"],
        )
        groups[(row["cluster"], row["namespace"], row["name"])] = row

    return list(groups.values())


def _list_accessible_cluster_ids(user: Optional[User]) -> List[str]:
    if should_use_real_k8s():
        try:
            payload = list_clusters_from_k8s()
            items = payload.get("items", [])
        except K8sCommandError:
            items = []
    else:
        items = list(CLUSTERS)
    if user:
        items = filter_clusters_for_user(user, items)
    return [item["id"] for item in items if item.get("id")]


def _discover_cluster_inventory_real(cluster_id: str) -> List[Dict[str, Any]]:
    access = resolve_cluster_access(cluster_id)
    if not access:
        return []
    now = datetime.now(timezone.utc).isoformat()
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    metrics_available = metrics_server_available(access)
    top_by_pod: Dict[Tuple[str, str], Dict[str, float]] = {}
    if metrics_available:
        top_by_pod = fetch_pod_top_metrics(access)
    app_usage_index = _build_app_usage_index(access, top_by_pod)

    from ..k8s_provider import _run_for_access

    resource_specs = [
        ("deployments", "Deployment"),
        ("statefulsets", "StatefulSet"),
        ("daemonsets", "DaemonSet"),
        ("jobs", "Job"),
        ("cronjobs", "CronJob"),
    ]

    services_output = _run_for_access(access, ["get", "services", "-A", "-o", "json"])
    svc_items = json.loads(services_output).get("items", [])
    services_by_ns: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for svc in svc_items:
        ns = svc.get("metadata", {}).get("namespace", "default")
        services_by_ns[ns].append(svc)

    for resource_kind, workload_type in resource_specs:
        try:
            output = _run_for_access(access, ["get", resource_kind, "-A", "-o", "json"])
        except K8sCommandError:
            continue
        for item in json.loads(output).get("items", []):
            meta = item.get("metadata", {})
            spec = item.get("spec", {})
            status = item.get("status", {})
            namespace = meta.get("namespace", "default")
            workload_name = meta.get("name", "unknown")
            labels = meta.get("labels") or {}
            template_labels = (spec.get("template") or {}).get("metadata", {}).get("labels") or {}
            merged_labels = {**template_labels, **labels}
            app_name = resolve_app_name(merged_labels, workload_name)

            if workload_type == "CronJob":
                desired = len(status.get("active", []) or [])
                ready = desired
            elif workload_type == "Job":
                desired = spec.get("completions") or 1
                succeeded = status.get("succeeded") or 0
                ready = succeeded
            elif workload_type == "DaemonSet":
                desired = status.get("desiredNumberScheduled") or 0
                ready = status.get("numberReady") or 0
            else:
                desired = spec.get("replicas") or status.get("replicas") or 0
                ready = status.get("readyReplicas") or status.get("numberReady") or 0

            containers = (spec.get("template") or {}).get("spec", {}).get("containers") or spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
            image = (containers[0] if containers else {}).get("image", "-")

            matched_svc = None
            for svc in services_by_ns.get(namespace, []):
                svc_name = svc.get("metadata", {}).get("name")
                selector = svc.get("spec", {}).get("selector") or {}
                if selector and all(
                    merged_labels.get(k) == v for k, v in selector.items()
                ):
                    matched_svc = svc
                    break
                if svc_name in (workload_name, app_name):
                    matched_svc = svc
            ports: List[int] = []
            service_name = "-"
            if matched_svc:
                service_name = matched_svc.get("metadata", {}).get("name", "-")
                ports = [int(p.get("port")) for p in matched_svc.get("spec", {}).get("ports", []) if p.get("port")]

            cpu_usage, memory_usage, cpu_cores, memory_mib = _usage_fields_for_app(
                namespace, app_name, metrics_available, app_usage_index
            )

            row = _build_row_from_workload(
                cluster_id=cluster_id,
                namespace=namespace,
                app_name=app_name,
                workload_type=workload_type,
                workload_name=workload_name,
                desired=int(desired or 0),
                ready=int(ready or 0),
                image=image,
                service_name=service_name,
                ports=ports,
                cpu_usage=cpu_usage,
                memory_usage=memory_usage,
                last_updated=meta.get("creationTimestamp") or now,
                status=compute_status(desired=int(desired or 0), ready=int(ready or 0)),
                workload_names=[workload_name],
            )
            row["_cpuCores"] = cpu_cores
            row["_memoryMib"] = memory_mib
            _merge_workload_into_group(groups, row)

    result: List[Dict[str, Any]] = []
    for row in groups.values():
        row.pop("_cpuCores", None)
        row.pop("_memoryMib", None)
        result.append(row)
    return result


WORKLOAD_LIST_KINDS = [
    ("deployments", "Deployment"),
    ("statefulsets", "StatefulSet"),
    ("daemonsets", "DaemonSet"),
    ("jobs", "Job"),
    ("cronjobs", "CronJob"),
    ("services", "Service"),
]


def list_namespace_workloads(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
) -> Tuple[List[Dict[str, Any]], Optional[str], int]:
    if user and not is_admin(user):
        if not can_access_namespace(user, cluster_id, namespace):
            return [], "Forbidden", 403

    workloads: List[Dict[str, Any]] = []

    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return [], "Cluster not found", 404
        from ..k8s_provider import _run_for_access

        for resource_kind, workload_type in WORKLOAD_LIST_KINDS:
            try:
                output = _run_for_access(
                    access,
                    ["get", resource_kind, "-n", namespace, "-o", "json"],
                )
            except K8sCommandError:
                continue
            for item in json.loads(output).get("items", []):
                meta = item.get("metadata", {})
                workloads.append(
                    {
                        "name": meta.get("name"),
                        "type": workload_type,
                        "namespace": namespace,
                        "labels": meta.get("labels") or {},
                        "createdAt": meta.get("creationTimestamp"),
                    }
                )
    else:
        ns_resources = NAMESPACE_RESOURCES.get(cluster_id, {}).get(namespace) or {}
        for resource_kind, workload_type in WORKLOAD_LIST_KINDS:
            key = resource_kind
            for item in ns_resources.get(key) or []:
                workloads.append(
                    {
                        "name": item.get("name"),
                        "type": workload_type,
                        "namespace": namespace,
                        "labels": item.get("labels") or {},
                        "createdAt": None,
                    }
                )

    workloads.sort(key=lambda w: (w.get("type") or "", w.get("name") or ""))
    return workloads, None, 200


def _merge_catalog_metadata(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .app_catalog_service import get_entry_for_inventory, list_active_entries

    entries = list_active_entries()
    entry_by_key: Dict[Tuple[str, str, str], Any] = {}
    for entry in entries:
        key = (entry.cluster_id, entry.namespace, entry.display_name)
        entry_by_key[key] = entry
        if entry.workload_name:
            wkey = (entry.cluster_id, entry.namespace, entry.workload_name)
            entry_by_key.setdefault(wkey, entry)

    merged_keys = set()
    result: List[Dict[str, Any]] = []

    for item in items:
        cluster_id = item.get("cluster") or item.get("clusterId")
        namespace = item.get("namespace")
        app_name = item.get("name")
        workload_names = item.get("workloadNames") or []

        entry = get_entry_for_inventory(cluster_id, namespace, app_name, workload_names[0] if workload_names else None)
        row = dict(item)
        if entry:
            row["ownerTeam"] = entry.owner_team or "Unassigned"
            row["environment"] = entry.environment or "Not set"
            row["criticality"] = entry.criticality or "Not set"
            row["documentationUrl"] = entry.documentation_url
            row["contactEmail"] = entry.contact_email or "Not set"
            row["tags"] = entry.tags or []
            row["source"] = entry.source if entry.source != "Registered" else (
                "Registered" if entry.source == "Registered" else item.get("source", "Discovered")
            )
            if entry.source == "Registered":
                row["source"] = "Registered"
            elif entry.source == "Deployed by KubeSight":
                row["source"] = "Deployed by KubeSight"
            elif entry.source == "Helm":
                row["source"] = "Helm"
            else:
                row["source"] = entry.source or "Discovered"
            row["catalogEntryId"] = entry.id
            if entry.release_name:
                row["releaseName"] = entry.release_name
            if entry.chart_name:
                row["chartName"] = entry.chart_name
            if entry.chart_version:
                row["chartVersion"] = entry.chart_version
            if entry.app_version:
                row["appVersion"] = entry.app_version
            if entry.helm_revision is not None:
                row["helmRevision"] = entry.helm_revision
            row["catalog"] = {
                "id": entry.id,
                "displayName": entry.display_name,
                "description": entry.description,
                "documentationUrl": entry.documentation_url,
                "contactEmail": entry.contact_email,
                "ownerTeam": entry.owner_team,
                "environment": entry.environment,
                "criticality": entry.criticality,
                "tags": entry.tags or [],
                "source": row["source"],
            }
            merged_keys.add((entry.cluster_id, entry.namespace, entry.display_name))
        result.append(row)

    for entry in entries:
        key = (entry.cluster_id, entry.namespace, entry.display_name)
        if key in merged_keys:
            continue
        inv_id = make_inventory_id(entry.cluster_id, entry.namespace, entry.display_name)
        result.append(
            {
                "id": inv_id,
                "name": entry.display_name,
                "cluster": entry.cluster_id,
                "clusterId": entry.cluster_id,
                "clusterName": _cluster_display_name(entry.cluster_id),
                "namespace": entry.namespace,
                "workloadType": entry.workload_type or "Unknown",
                "workloadNames": [entry.workload_name] if entry.workload_name else [],
                "status": "Unknown",
                "replicas": 0,
                "readyReplicas": 0,
                "image": "-",
                "versionTag": "-",
                "service": "-",
                "ports": [],
                "cpuUsage": "-",
                "memoryUsage": "-",
                "lastUpdated": entry.updated_at.isoformat() if entry.updated_at else "",
                "ownerTeam": entry.owner_team or "Unassigned",
                "environment": entry.environment or "Not set",
                "criticality": entry.criticality or "Not set",
                "documentationUrl": entry.documentation_url,
                "contactEmail": entry.contact_email or "Not set",
                "tags": entry.tags or [],
                "source": entry.source or "Registered",
                "catalogEntryId": entry.id,
                "catalog": {
                    "id": entry.id,
                    "displayName": entry.display_name,
                    "description": entry.description,
                    "documentationUrl": entry.documentation_url,
                    "contactEmail": entry.contact_email,
                    "ownerTeam": entry.owner_team,
                    "environment": entry.environment,
                    "criticality": entry.criticality,
                    "tags": entry.tags or [],
                    "source": entry.source,
                },
            }
        )

    return result


def _mock_helm_inventory_items(cluster_id: str) -> List[Dict[str, Any]]:
    from .helm_service import helm_release_to_inventory_row

    releases = HELM_RELEASES.get(cluster_id, [])
    return [helm_release_to_inventory_row(cluster_id, release) for release in releases]


def _discover_helm_inventory_real(cluster_id: str) -> List[Dict[str, Any]]:
    from .helm_service import helm_release_to_inventory_row, list_releases

    releases = list_releases(cluster_id)
    return [helm_release_to_inventory_row(cluster_id, release) for release in releases]


def _merge_helm_releases(items: List[Dict[str, Any]], helm_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key = {(i.get("cluster"), i.get("namespace"), i.get("name")): i for i in items}
    result = list(items)

    for helm_row in helm_items:
        key = (helm_row.get("cluster"), helm_row.get("namespace"), helm_row.get("name"))
        existing = by_key.get(key)
        if existing:
            existing.update({
                "source": "Helm",
                "releaseName": helm_row.get("releaseName"),
                "chartName": helm_row.get("chartName"),
                "chartVersion": helm_row.get("chartVersion"),
                "appVersion": helm_row.get("appVersion"),
                "helmRevision": helm_row.get("helmRevision"),
                "helmStatus": helm_row.get("helmStatus"),
                "helm": helm_row.get("helm"),
            })
        else:
            result.append(helm_row)
            by_key[key] = helm_row

    return result


def list_inventory(user: Optional[User], filters: Optional[Dict[str, str]] = None) -> Tuple[List[Dict[str, Any]], Optional[str], int]:
    filters = filters or {}
    items: List[Dict[str, Any]] = []
    cluster_filter = (filters.get("cluster") or filters.get("clusterId") or "").strip()

    if should_use_real_k8s() and not cluster_filter:
        for cluster_id in _list_accessible_cluster_ids(user):
            try:
                items.extend(_discover_cluster_inventory_real(cluster_id))
            except K8sCommandError:
                continue
    elif should_use_real_k8s(cluster_filter) and cluster_filter:
        try:
            items = _discover_cluster_inventory_real(cluster_filter)
        except K8sCommandError as exc:
            return [], str(exc), 503
    else:
        items = _mock_inventory_items()
        if cluster_filter:
            items = [i for i in items if i.get("cluster") == cluster_filter]

    helm_items: List[Dict[str, Any]] = []
    if should_use_real_k8s() and not cluster_filter:
        for cluster_id in _list_accessible_cluster_ids(user):
            try:
                helm_items.extend(_discover_helm_inventory_real(cluster_id))
            except Exception:
                continue
    elif should_use_real_k8s(cluster_filter) and cluster_filter:
        try:
            helm_items = _discover_helm_inventory_real(cluster_filter)
        except Exception:
            helm_items = []
    else:
        clusters = [cluster_filter] if cluster_filter else [c.get("id") for c in CLUSTERS if c.get("id")]
        for cluster_id in clusters:
            helm_items.extend(_mock_helm_inventory_items(cluster_id))

    items = _merge_helm_releases(items, helm_items)

    if user:
        if not is_admin(user):
            allowed_clusters = set(_list_accessible_cluster_ids(user))
            items = [i for i in items if i.get("cluster") in allowed_clusters]
            for cluster_id in allowed_clusters:
                if should_use_real_k8s(cluster_id):
                    continue
                ns_list = NAMESPACES.get(cluster_id, [])
                if user:
                    ns_list = filter_namespaces_for_user(user, cluster_id, ns_list)
                allowed_ns = {ns.get("name") for ns in ns_list}
                if allowed_ns:
                    items = [
                        i
                        for i in items
                        if i.get("cluster") != cluster_id or i.get("namespace") in allowed_ns
                    ]
        items = filter_inventory_for_user(user, items)

    items = _merge_catalog_metadata(items)
    items = _apply_list_filters(items, filters)
    items.sort(key=lambda row: (row.get("cluster", ""), row.get("namespace", ""), row.get("name", "")))
    return items, None, 200


def summarize_inventory(items: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"applications": len(items), "healthy": 0, "warning": 0, "critical": 0, "unknown": 0}
    for item in items:
        status = (item.get("status") or "Unknown").lower()
        if status == "healthy":
            summary["healthy"] += 1
        elif status == "warning":
            summary["warning"] += 1
        elif status == "critical":
            summary["critical"] += 1
        else:
            summary["unknown"] += 1
    return summary


def get_inventory_summary(user: Optional[User], cluster_id: Optional[str] = None) -> Dict[str, int]:
    filters: Dict[str, str] = {}
    if cluster_id:
        filters["cluster"] = cluster_id
    items, _, _ = list_inventory(user, filters)
    return summarize_inventory(items)


def get_dashboard_inventory_summary(
    user: Optional[User], cluster_id: Optional[str] = None
) -> Dict[str, int]:
    """Fast dashboard counts from catalog metadata — avoids kubectl inventory discovery."""
    if not cluster_id:
        return {"applications": 0, "healthy": 0, "warning": 0, "critical": 0, "unknown": 0}

    from ..access_engine import can_access_namespace, is_admin
    from ..services.app_catalog_service import list_active_entries

    entries = list_active_entries(cluster_id=cluster_id)
    items: List[Dict[str, Any]] = [
        {
            "cluster": entry.cluster_id,
            "namespace": entry.namespace,
            "name": entry.display_name,
            "status": "Unknown",
        }
        for entry in entries
    ]

    if user:
        if not is_admin(user):
            allowed_clusters = set(_list_accessible_cluster_ids(user))
            if cluster_id not in allowed_clusters:
                return summarize_inventory([])
            items = [
                item
                for item in items
                if can_access_namespace(user, cluster_id, item.get("namespace") or "")
            ]
        items = filter_inventory_for_user(user, items)

    return summarize_inventory(items)


def _filter_detail_resources(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    resources: Dict[str, Any],
) -> Dict[str, Any]:
    from ..access_engine import filter_namespace_resources

    filtered = filter_namespace_resources(user, cluster_id, resources) if user else resources
    workloads = []
    for dep in filtered.get("deployments") or []:
        name = dep.get("name")
        if not name:
            continue
        ready, desired = _parse_replica_counts(dep.get("ready"), dep.get("desired"))
        workloads.append(
            {
                "name": name,
                "type": "Deployment",
                "desired": desired,
                "ready": ready,
                "image": dep.get("image"),
                "age": dep.get("age"),
            }
        )
    pods = []
    for pod in filtered.get("pods") or []:
        pod_copy = dict(pod)
        pod_copy["canViewLogs"] = bool(pod_copy.get("canViewLogs", True))
        if user and not can_view_logs(user, cluster_id, namespace, pod.get("name") or ""):
            pod_copy["canViewLogs"] = False
            actions = [a for a in (pod_copy.get("actions") or []) if a != "logs"]
            pod_copy["actions"] = actions
        pods.append(pod_copy)

    services = filtered.get("services") or []
    return {
        "workloads": workloads,
        "pods": pods,
        "services": services,
        "ingress": filtered.get("ingress") or [],
        "configMaps": filtered.get("configMaps") or [],
        "secrets": filtered.get("secrets") or [],
    }


def _apply_catalog_entry_to_row(row: Dict[str, Any], entry: Any) -> Dict[str, Any]:
    updated = dict(row)
    updated["ownerTeam"] = entry.owner_team or "Unassigned"
    updated["environment"] = entry.environment or "Not set"
    updated["criticality"] = entry.criticality or "Not set"
    updated["documentationUrl"] = entry.documentation_url
    updated["contactEmail"] = entry.contact_email or "Not set"
    updated["tags"] = entry.tags or []
    if entry.source == "Registered":
        updated["source"] = "Registered"
    elif entry.source == "Deployed by KubeSight":
        updated["source"] = "Deployed by KubeSight"
    elif entry.source == "Helm":
        updated["source"] = "Helm"
    else:
        updated["source"] = entry.source or updated.get("source") or "Discovered"
    updated["catalogEntryId"] = entry.id
    if entry.release_name:
        updated["releaseName"] = entry.release_name
    if entry.chart_name:
        updated["chartName"] = entry.chart_name
    if entry.chart_version:
        updated["chartVersion"] = entry.chart_version
    if entry.app_version:
        updated["appVersion"] = entry.app_version
    if entry.helm_revision is not None:
        updated["helmRevision"] = entry.helm_revision
    updated["catalog"] = {
        "id": entry.id,
        "displayName": entry.display_name,
        "description": entry.description,
        "documentationUrl": entry.documentation_url,
        "contactEmail": entry.contact_email,
        "ownerTeam": entry.owner_team,
        "environment": entry.environment,
        "criticality": entry.criticality,
        "tags": entry.tags or [],
        "source": updated["source"],
    }
    return updated


def _build_list_row_from_catalog_entry(
    entry: Any,
    cluster_id: str,
    namespace: str,
    app_name: str,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": make_inventory_id(cluster_id, namespace, app_name),
        "name": app_name,
        "cluster": cluster_id,
        "clusterId": cluster_id,
        "clusterName": _cluster_display_name(cluster_id),
        "namespace": namespace,
        "workloadType": entry.workload_type or "Unknown",
        "workloadNames": [entry.workload_name] if entry.workload_name else [],
        "status": "Unknown",
        "replicas": 0,
        "readyReplicas": 0,
        "image": "-",
        "versionTag": "-",
        "service": "-",
        "ports": [],
        "cpuUsage": "-",
        "memoryUsage": "-",
        "lastUpdated": entry.updated_at.isoformat() if entry.updated_at else now,
        "source": entry.source or "Registered",
        "catalogEntryId": entry.id,
    }
    return _apply_catalog_entry_to_row(row, entry)


def _build_list_row_from_filtered_resources(
    cluster_id: str,
    namespace: str,
    app_name: str,
    resources: Dict[str, Any],
) -> Dict[str, Any]:
    deployments = resources.get("deployments") or []
    services = resources.get("services") or []
    pods = resources.get("pods") or []
    workload_names = [dep.get("name") for dep in deployments if dep.get("name")]
    primary_name = next((name for name in workload_names if name == app_name), workload_names[0] if workload_names else app_name)
    primary_dep = next((dep for dep in deployments if dep.get("name") == primary_name), deployments[0] if deployments else {})
    ready, desired = _parse_replica_counts(primary_dep.get("ready"), primary_dep.get("desired"))
    matched_svc = next(
        (svc for svc in services if svc.get("name") in {primary_name, app_name}),
        services[0] if services else {},
    )
    ports_raw = matched_svc.get("ports") or ""
    ports = [int(p) for p in str(ports_raw).split(",") if str(p).strip().isdigit()]
    total_cpu = 0.0
    total_mem = 0.0
    metrics_available = False
    for pod in pods:
        cpu_raw = pod.get("cpuUsage")
        mem_raw = pod.get("memoryUsage")
        if isinstance(cpu_raw, str) and cpu_raw not in ("-", "Metrics unavailable"):
            metrics_available = True
            try:
                total_cpu += float(cpu_raw)
            except ValueError:
                pass
        if isinstance(mem_raw, str) and "Mi" in mem_raw:
            metrics_available = True
            try:
                total_mem += float(mem_raw.replace("Mi", ""))
            except ValueError:
                pass
    cpu_usage = f"{total_cpu:.3f} cores" if metrics_available and total_cpu else ("-" if pods else "Metrics unavailable")
    memory_usage = f"{int(total_mem)}Mi" if metrics_available and total_mem else ("-" if pods else "Metrics unavailable")
    now = datetime.now(timezone.utc).isoformat()
    return _build_row_from_workload(
        cluster_id=cluster_id,
        namespace=namespace,
        app_name=app_name,
        workload_type="Deployment",
        workload_name=primary_name,
        desired=desired,
        ready=ready,
        image=primary_dep.get("image") or "-",
        service_name=matched_svc.get("name") or "-",
        ports=ports,
        cpu_usage=cpu_usage,
        memory_usage=memory_usage,
        last_updated=now,
        status=compute_status(desired=desired, ready=ready),
        workload_names=workload_names or [primary_name],
    )


def _find_inventory_row_for_detail(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    app_name: str,
    inventory_id: str,
    resources: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    from .app_catalog_service import get_entry_for_inventory

    entry = get_entry_for_inventory(cluster_id, namespace, app_name)
    if resources and (
        resources.get("deployments") or resources.get("pods") or resources.get("services")
    ):
        row = _build_list_row_from_filtered_resources(cluster_id, namespace, app_name, resources)
        if entry:
            row = _apply_catalog_entry_to_row(row, entry)
        return row
    if entry:
        return _build_list_row_from_catalog_entry(entry, cluster_id, namespace, app_name)

    if should_use_real_k8s(cluster_id):
        from .helm_service import get_release_detail

        helm_detail = get_release_detail(cluster_id, namespace, app_name)
        if helm_detail:
            now = datetime.now(timezone.utc).isoformat()
            return {
                "id": make_inventory_id(cluster_id, namespace, app_name),
                "name": app_name,
                "cluster": cluster_id,
                "clusterId": cluster_id,
                "clusterName": _cluster_display_name(cluster_id),
                "namespace": namespace,
                "workloadType": "Helm Release",
                "workloadNames": [],
                "status": helm_detail.get("status") or "Unknown",
                "source": "Helm",
                "releaseName": helm_detail.get("releaseName"),
                "chartName": helm_detail.get("chartName"),
                "chartVersion": helm_detail.get("chartVersion"),
                "appVersion": helm_detail.get("appVersion"),
                "helmRevision": helm_detail.get("revision"),
                "helmStatus": helm_detail.get("status"),
                "helm": helm_detail,
                "lastUpdated": now,
            }
        return None

    items, err, code = list_inventory(
        user,
        {"cluster": cluster_id, "namespace": namespace, "name": app_name},
    )
    if err or code != 200:
        return None
    return next(
        (
            item
            for item in items
            if _inventory_ids_equal(item.get("id") or "", inventory_id)
            or (
                item.get("name") == app_name
                and item.get("namespace") == namespace
                and (item.get("cluster") == cluster_id or item.get("clusterId") == cluster_id)
            )
        ),
        None,
    )


def _mock_detail(cluster_id: str, namespace: str, app_name: str) -> Optional[Dict[str, Any]]:
    list_row = next(
        (
            i
            for i in _mock_inventory_items()
            if i["cluster"] == cluster_id and i["namespace"] == namespace and i["name"] == app_name
        ),
        None,
    )
    if not list_row:
        return None

    extras_key = f"{cluster_id}:{namespace}:{app_name}"
    extra = INVENTORY_DETAIL_EXTRAS.get(extras_key, {})
    ns_resources = dict(NAMESPACE_RESOURCES.get(cluster_id, {}).get(namespace) or {})
    if extra:
        ns_resources = {
            "namespace": namespace,
            "deployments": extra.get("deployments") or ns_resources.get("deployments", []),
            "services": extra.get("services") or ns_resources.get("services", []),
            "pods": extra.get("pods") or ns_resources.get("pods", []),
        }
    ns_resources["namespace"] = namespace

    workload_names = list_row.get("workloadNames") or [app_name]
    if NAMESPACE_RESOURCES.get(cluster_id, {}).get(namespace) and not extra.get("deployments"):
        ns_resources = _filter_resources_for_app(ns_resources, app_name, workload_names)

    now = datetime.now(timezone.utc).isoformat()
    filtered = _filter_detail_resources(None, cluster_id, namespace, ns_resources)

    related_alerts = [
        a
        for a in ALERTS
        if a.get("clusterId") == cluster_id and (a.get("namespace") == namespace)
    ]

    return {
        "id": list_row["id"],
        "summary": {
            "applicationName": app_name,
            "cluster": cluster_id,
            "clusterName": _cluster_display_name(cluster_id),
            "namespace": namespace,
            "type": list_row.get("workloadType"),
            "status": list_row.get("status"),
            "replicas": list_row.get("replicas"),
            "readyReplicas": list_row.get("readyReplicas"),
            "image": list_row.get("image"),
            "version": list_row.get("versionTag"),
            "creationTime": extra.get("creationTime", now),
            "lastUpdated": list_row.get("lastUpdated", now),
            "ownerTeam": list_row.get("ownerTeam"),
            "environment": list_row.get("environment"),
            "criticality": list_row.get("criticality"),
            "documentationUrl": list_row.get("documentationUrl"),
            "contactEmail": list_row.get("contactEmail"),
            "tags": list_row.get("tags") or [],
            "source": list_row.get("source"),
            "catalogEntryId": list_row.get("catalogEntryId"),
        },
        "catalog": list_row.get("catalog") or {},
        "workloads": filtered["workloads"],
        "pods": filtered["pods"],
        "services": filtered["services"],
        "ingress": extra.get("ingress", []),
        "configMaps": extra.get("configMaps", []),
        "secrets": extra.get("secrets", []),
        "metrics": {
            "available": True,
            "perPod": [
                {
                    "pod": p.get("name"),
                    "cpu": p.get("cpuUsage"),
                    "memory": p.get("memoryUsage"),
                }
                for p in filtered["pods"]
            ],
            "aggregate": {
                "cpu": list_row.get("cpuUsage"),
                "memory": list_row.get("memoryUsage"),
            },
        },
        "alerts": related_alerts,
        "events": extra.get("events", []),
    }


def get_inventory_detail(user: Optional[User], inventory_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    parsed = parse_inventory_id(inventory_id)
    if not parsed:
        return None, "Invalid inventory id", 400
    cluster_id, namespace, app_name = parsed

    if user and not is_admin(user):
        if not can_access_namespace(user, cluster_id, namespace):
            return None, "Forbidden", 403

    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return None, "Cluster not found", 404
        try:
            namespace_resources = namespace_resources_from_k8s(access, namespace)
        except K8sCommandError as exc:
            return None, str(exc), 503
        list_row = _find_inventory_row_for_detail(
            user,
            cluster_id,
            namespace,
            app_name,
            inventory_id,
            resources=_filter_resources_for_app(namespace_resources, app_name, [app_name]),
        )
        if not list_row:
            return None, "Application not found", 404

        resolved_app_name = list_row.get("name") or app_name
        canonical_inventory_id = list_row.get("id") or inventory_id
        catalog = list_row.get("catalog")
        catalog_meta = catalog or {}
        workload_names = list_row.get("workloadNames") or [resolved_app_name]
        scoped_resources = _filter_resources_for_app(namespace_resources, resolved_app_name, workload_names)
        filtered = _filter_detail_resources(user, cluster_id, namespace, scoped_resources)
        metrics_available = any(
            isinstance(pod.get("cpuUsage"), str) and pod.get("cpuUsage") not in ("-", "Metrics unavailable")
            for pod in filtered["pods"]
        )
        metrics_payload = {
            "available": metrics_available,
            "perPod": [],
            "aggregate": {"cpu": "Metrics unavailable", "memory": "Metrics unavailable"},
        }
        if metrics_available:
            per_pod = []
            total_cpu = 0.0
            total_mem = 0.0
            for pod in filtered["pods"]:
                cpu_raw = pod.get("cpuUsage")
                mem_raw = pod.get("memoryUsage")
                per_pod.append({"pod": pod.get("name"), "cpu": cpu_raw, "memory": mem_raw})
                if isinstance(cpu_raw, str) and cpu_raw not in ("-", "Metrics unavailable"):
                    try:
                        total_cpu += float(cpu_raw)
                    except ValueError:
                        pass
                if isinstance(mem_raw, str) and "Mi" in mem_raw:
                    try:
                        total_mem += float(mem_raw.replace("Mi", ""))
                    except ValueError:
                        pass
            metrics_payload["perPod"] = per_pod
            metrics_payload["aggregate"] = {
                "cpu": f"{total_cpu:.3f} cores" if total_cpu else "-",
                "memory": f"{int(total_mem)}Mi" if total_mem else "-",
            }

        alerts = []

        detail = {
            "id": canonical_inventory_id,
            "summary": {
                "applicationName": resolved_app_name,
                "cluster": cluster_id,
                "clusterName": _cluster_display_name(cluster_id),
                "namespace": namespace,
                "type": list_row.get("workloadType"),
                "status": list_row.get("status"),
                "replicas": list_row.get("replicas"),
                "readyReplicas": list_row.get("readyReplicas"),
                "image": list_row.get("image"),
                "version": list_row.get("versionTag"),
                "creationTime": list_row.get("lastUpdated"),
                "lastUpdated": list_row.get("lastUpdated"),
                "ownerTeam": list_row.get("ownerTeam"),
                "environment": list_row.get("environment"),
                "criticality": list_row.get("criticality"),
                "documentationUrl": list_row.get("documentationUrl"),
                "contactEmail": list_row.get("contactEmail"),
                "tags": list_row.get("tags") or [],
                "source": list_row.get("source"),
                "catalogEntryId": list_row.get("catalogEntryId"),
            },
            "catalog": catalog_meta,
            "workloads": filtered["workloads"],
            "pods": filtered["pods"],
            "services": filtered["services"],
            "ingress": filtered.get("ingress") or [],
            "configMaps": filtered.get("configMaps") or [],
            "secrets": filtered.get("secrets") or [],
            "metrics": metrics_payload,
            "alerts": alerts,
            "events": [],
        }
        if list_row.get("source") == "Helm" or list_row.get("helm"):
            from .helm_service import get_release_detail
            helm_detail = get_release_detail(cluster_id, namespace, resolved_app_name)
            if helm_detail:
                detail["helm"] = helm_detail
                detail["summary"]["releaseName"] = helm_detail.get("releaseName")
                detail["summary"]["chartName"] = helm_detail.get("chartName")
                detail["summary"]["chartVersion"] = helm_detail.get("chartVersion")
                detail["summary"]["appVersion"] = helm_detail.get("appVersion")
                detail["summary"]["helmRevision"] = helm_detail.get("revision")
                detail["summary"]["helmStatus"] = helm_detail.get("status")
        return detail, None, 200

    list_row = _find_inventory_row_for_detail(user, cluster_id, namespace, app_name, inventory_id)
    if not list_row:
        return None, "Application not found", 404

    resolved_app_name = list_row.get("name") or app_name
    canonical_inventory_id = list_row.get("id") or inventory_id
    catalog = list_row.get("catalog")
    catalog_meta = catalog or {}

    detail = _mock_detail(cluster_id, namespace, resolved_app_name)
    if not detail and list_row.get("source") == "Helm":
        now = datetime.now(timezone.utc).isoformat()
        helm_key = f"{cluster_id}:{namespace}:{app_name}"
        helm_detail = HELM_RELEASE_DETAILS.get(helm_key) or list_row.get("helm") or {}
        detail = {
            "id": inventory_id,
            "summary": {
                "applicationName": app_name,
                "cluster": cluster_id,
                "clusterName": _cluster_display_name(cluster_id),
                "namespace": namespace,
                "type": "Helm Release",
                "status": list_row.get("status"),
                "source": "Helm",
                "releaseName": list_row.get("releaseName"),
                "chartName": list_row.get("chartName"),
                "chartVersion": list_row.get("chartVersion"),
                "appVersion": list_row.get("appVersion"),
                "helmRevision": list_row.get("helmRevision"),
                "helmStatus": list_row.get("helmStatus"),
                "lastUpdated": list_row.get("lastUpdated", now),
            },
            "catalog": list_row.get("catalog") or {},
            "helm": helm_detail,
            "workloads": [],
            "pods": [],
            "services": [],
            "ingress": [],
            "configMaps": [],
            "secrets": [],
            "metrics": {"available": False, "perPod": [], "aggregate": {"cpu": "-", "memory": "-"}},
            "alerts": [],
            "events": [],
        }
    if not detail:
        return None, "Application not found", 404

    if list_row.get("catalog"):
        detail["catalog"] = list_row["catalog"]
        detail["summary"]["ownerTeam"] = list_row.get("ownerTeam")
        detail["summary"]["environment"] = list_row.get("environment")
        detail["summary"]["criticality"] = list_row.get("criticality")
        detail["summary"]["documentationUrl"] = list_row.get("documentationUrl")
        detail["summary"]["contactEmail"] = list_row.get("contactEmail")
        detail["summary"]["tags"] = list_row.get("tags") or []
        detail["summary"]["source"] = list_row.get("source")
        detail["summary"]["catalogEntryId"] = list_row.get("catalogEntryId")

    if user:
        detail["pods"] = _filter_detail_resources(user, cluster_id, namespace, NAMESPACE_RESOURCES.get(cluster_id, {}).get(namespace, {}) or {"namespace": namespace, "pods": detail.get("pods", []), "deployments": [], "services": []})["pods"]
        if user_has_permission(user, "alerts:view"):
            detail["alerts"] = filter_alerts_for_user(user, detail.get("alerts") or [])
        else:
            detail["alerts"] = []
        for pod in detail.get("pods") or []:
            pod["canViewLogs"] = can_view_logs(user, cluster_id, namespace, pod.get("name") or "")

    if not can_access_inventory_app(user, list_row):
        return None, "Forbidden", 403

    if list_row.get("source") == "Helm" or list_row.get("helm"):
        helm_key = f"{cluster_id}:{namespace}:{app_name}"
        helm_detail = HELM_RELEASE_DETAILS.get(helm_key)
        if not helm_detail and should_use_real_k8s(cluster_id):
            from .helm_service import get_release_detail
            helm_detail = get_release_detail(cluster_id, namespace, app_name)
        if helm_detail:
            detail["helm"] = helm_detail
            detail["summary"]["releaseName"] = helm_detail.get("releaseName")
            detail["summary"]["chartName"] = helm_detail.get("chartName")
            detail["summary"]["chartVersion"] = helm_detail.get("chartVersion")
            detail["summary"]["appVersion"] = helm_detail.get("appVersion")
            detail["summary"]["helmRevision"] = helm_detail.get("revision")
            detail["summary"]["helmStatus"] = helm_detail.get("status")

    return detail, None, 200
