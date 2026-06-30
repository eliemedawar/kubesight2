"""Fine-grained access rule evaluation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from flask import g
from sqlalchemy import inspect as sa_inspect

from .models import AccessRule, User

RESOURCE_SPECIFICITY = {
    "cluster": 10,
    "namespace": 20,
    "deployment": 30,
    "replicaset": 30,
    "statefulset": 30,
    "daemonset": 30,
    "job": 30,
    "cronjob": 30,
    "pod": 30,
    "service": 30,
    "container": 40,
    "service_port": 45,
}

# Broad management keys imply granular user/role permissions.
PERMISSION_MANAGED_BY = {
    "users:view": ("users:manage",),
    "users:create": ("users:manage",),
    "users:update": ("users:manage",),
    "users:disable": ("users:manage",),
    "roles:view": ("roles:manage", "users:manage"),
    "roles:manage": ("users:manage",),
}

PERMISSION_ALIASES = {
    "namespaces:view": ("namespaces:view", "resources:view"),
    "pods:view": ("pods:view", "resources:view"),
    "deployments:view": ("deployments:view", "resources:view"),
    "replicasets:view": ("replicasets:view", "deployments:view", "resources:view"),
    "statefulsets:view": ("statefulsets:view", "deployments:view", "resources:view"),
    "daemonsets:view": ("daemonsets:view", "resources:view"),
    "jobs:view": ("jobs:view", "resources:view"),
    "cronjobs:view": ("cronjobs:view", "resources:view"),
    "services:view": ("services:view", "resources:view"),
    "resources:view": (
        "resources:view",
        "pods:view",
        "deployments:view",
        "replicasets:view",
        "statefulsets:view",
        "daemonsets:view",
        "jobs:view",
        "cronjobs:view",
        "services:view",
    ),
    "inventory:view": ("inventory:view", "resources:view"),
}

NAMESPACE_RESOURCE_LIST_KEYS = (
    "pods",
    "deployments",
    "replicasets",
    "statefulsets",
    "daemonsets",
    "jobs",
    "cronjobs",
    "services",
    "configmaps",
    "secrets",
)


def is_admin(user: User) -> bool:
    if not user.role:
        return False
    if user.role.name == "admin":
        return True
    granted = {perm.key for perm in user.role.permissions}
    from .rbac_data import ALL_PERMISSION_KEYS

    return set(ALL_PERMISSION_KEYS).issubset(granted)


def get_user_permission_keys(user: User) -> Set[str]:
    if not user.role:
        return set()
    return {perm.key for perm in user.role.permissions}


def user_has_permission(user: User, permission_key: str) -> bool:
    if is_admin(user):
        return True
    keys = get_user_permission_keys(user)
    if permission_key in keys:
        return True
    for manager in PERMISSION_MANAGED_BY.get(permission_key, ()):
        if manager in keys:
            return True
    for alias in PERMISSION_ALIASES.get(permission_key, ()):
        if alias in keys:
            return True
    return False


def _role_allows_permission(user: User, permission_key: str) -> bool:
    return user_has_permission(user, permission_key)


def _access_rules_cache() -> Dict[int, List[AccessRule]]:
    cache = getattr(g, "_access_rules_by_user_id", None)
    if cache is None:
        cache = {}
        g._access_rules_by_user_id = cache
    return cache


def invalidate_access_rules_cache(user_id: Optional[int] = None) -> None:
    """Drop cached AccessRule rows after mutations or for all users in this request."""
    try:
        cache = getattr(g, "_access_rules_by_user_id", None)
    except RuntimeError:
        return
    if cache is None:
        return
    if user_id is None:
        cache.clear()
    else:
        cache.pop(user_id, None)


def _load_rules(user: User) -> List[AccessRule]:
    try:
        cache = _access_rules_cache()
        cached = cache.get(user.id)
        if cached is not None:
            return cached
        if "access_rules" not in sa_inspect(user).unloaded:
            rules = list(user.access_rules)
        else:
            rules = AccessRule.query.filter_by(user_id=user.id).all()
        cache[user.id] = rules
        return rules
    except RuntimeError:
        return AccessRule.query.filter_by(user_id=user.id).all()


def _uses_legacy_only(user: User, rules: List[AccessRule]) -> bool:
    return len(rules) == 0


def _legacy_cluster_ids(user: User) -> Set[str]:
    return {row.cluster_id for row in user.cluster_access_entries if row.can_view}


def _legacy_namespace_pairs(user: User) -> Set[Tuple[str, str]]:
    return {
        (row.cluster_id, row.namespace)
        for row in user.namespace_access_entries
        if row.can_view
    }


def rule_specificity(rule: AccessRule) -> int:
    base = RESOURCE_SPECIFICITY.get(rule.resource_type or "cluster", 5)
    if rule.resource_name:
        base += 5
    if rule.container_name:
        base += 3
    if rule.port is not None:
        base += 2
    if rule.namespace:
        base += 1
    return base


def _rule_permission_matches(rule_key: str, requested_key: str) -> bool:
    if rule_key == requested_key:
        return True
    return rule_key in PERMISSION_ALIASES.get(requested_key, ())


def _rule_matches(
    rule: AccessRule,
    *,
    cluster_id: str,
    namespace: Optional[str],
    resource_type: Optional[str],
    resource_name: Optional[str],
    container_name: Optional[str],
    port: Optional[int],
    permission_key: str,
) -> bool:
    if rule.cluster_id != cluster_id:
        return False
    if not _rule_permission_matches(rule.permission_key, permission_key):
        return False

    rule_ns = (rule.namespace or "").strip() or None
    req_ns = (namespace or "").strip() or None

    rule_rt = (rule.resource_type or "cluster").strip()
    req_rt = (resource_type or "").strip() or None

    if rule_ns and req_ns and rule_ns != req_ns:
        return False
    if rule_ns and not req_ns and rule_rt not in ("cluster",):
        return False

    if rule_rt == "cluster":
        return True

    if rule_rt == "namespace":
        return req_ns == rule_ns if rule_ns else bool(req_ns)

    if not req_rt:
        return False
    if rule_rt != req_rt:
        return False

    rule_name = (rule.resource_name or "").strip() or None
    req_name = (resource_name or "").strip() or None
    if rule_name and req_name and rule_name != req_name:
        return False
    if rule_name and not req_name:
        return False

    if rule_rt in ("pod", "container") and rule.container_name:
        req_container = (container_name or "").strip() or None
        if req_container and rule.container_name != req_container:
            return False

    if rule_rt == "service_port":
        if rule.port is not None and port is not None and int(rule.port) != int(port):
            return False
        if rule.port is not None and port is None:
            return False

    return True


def _rules_for_evaluation(
    rules: List[AccessRule],
    *,
    cluster_id: str,
    namespace: Optional[str],
    resource_type: Optional[str],
) -> List[AccessRule]:
    """When a namespace has named resource grants, ignore broad cluster-level rules there."""
    if not namespace or not resource_type:
        return rules
    has_named = any(
        rule.cluster_id == cluster_id
        and (rule.namespace or "") == namespace
        and (rule.resource_type or "") == resource_type
        and (rule.resource_name or "").strip()
        and rule.effect == "allow"
        for rule in rules
    )
    if not has_named:
        return rules
    return [
        rule
        for rule in rules
        if (rule.resource_type or "cluster") != "cluster" or (rule.resource_name or "").strip()
    ]


def evaluate_access(
    user: User,
    *,
    cluster_id: str,
    permission_key: str,
    namespace: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_name: Optional[str] = None,
    container_name: Optional[str] = None,
    port: Optional[int] = None,
) -> bool:
    if is_admin(user):
        return True
    if not _role_allows_permission(user, permission_key):
        return False

    rules = _load_rules(user)
    if _uses_legacy_only(user, rules):
        return _legacy_evaluate(
            user,
            cluster_id=cluster_id,
            permission_key=permission_key,
            namespace=namespace,
            resource_type=resource_type,
            resource_name=resource_name,
        )

    scoped_rules = _rules_for_evaluation(
        rules,
        cluster_id=cluster_id,
        namespace=namespace,
        resource_type=resource_type,
    )
    matching = [
        r
        for r in scoped_rules
        if _rule_matches(
            r,
            cluster_id=cluster_id,
            namespace=namespace,
            resource_type=resource_type,
            resource_name=resource_name,
            container_name=container_name,
            port=port,
            permission_key=permission_key,
        )
    ]
    if not matching:
        return False

    matching.sort(key=rule_specificity, reverse=True)
    top_score = rule_specificity(matching[0])
    top = [r for r in matching if rule_specificity(r) == top_score]
    if any(r.effect == "deny" for r in top):
        return False
    return any(r.effect == "allow" for r in top)


def _legacy_evaluate(
    user: User,
    *,
    cluster_id: str,
    permission_key: str,
    namespace: Optional[str],
    resource_type: Optional[str],
    resource_name: Optional[str],
) -> bool:
    clusters = _legacy_cluster_ids(user)
    if cluster_id not in clusters:
        return False
    ns_pairs = _legacy_namespace_pairs(user)
    cluster_ns = {ns for cid, ns in ns_pairs if cid == cluster_id}
    if not cluster_ns:
        return True
    if not namespace:
        return permission_key == "clusters:view"
    if namespace not in cluster_ns:
        return False
    if resource_type and resource_name:
        return True
    return True


def can_access_cluster(user: User, cluster_id: str) -> bool:
    return evaluate_access(user, cluster_id=cluster_id, permission_key="clusters:view", resource_type="cluster")


def can_access_namespace(user: User, cluster_id: str, namespace: str) -> bool:
    if is_admin(user):
        return True
    rules = _load_rules(user)
    if rules:
        scoped_namespaces = {
            (rule.namespace or "").strip()
            for rule in rules
            if rule.cluster_id == cluster_id
            and rule.effect == "allow"
            and rule.permission_key in ("namespaces:view", "resources:view")
            and (rule.resource_type or "cluster") in ("namespace", "pod", "deployment", "service")
            and (rule.namespace or "").strip()
        }
        if scoped_namespaces:
            return namespace in scoped_namespaces
    return evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="namespaces:view",
        resource_type="namespace",
    )


def _pod_matches_deployment_name(pod_name: str, deployment_name: str) -> bool:
    if not pod_name or not deployment_name:
        return False
    if pod_name == deployment_name:
        return True
    return pod_name.startswith(f"{deployment_name}-")


def can_access_resource(
    user: User,
    cluster_id: str,
    namespace: str,
    resource_type: str,
    resource_name: str,
) -> bool:
    perm_map = {
        "pod": "pods:view",
        "deployment": "deployments:view",
        "replicaset": "replicasets:view",
        "statefulset": "statefulsets:view",
        "daemonset": "daemonsets:view",
        "job": "jobs:view",
        "cronjob": "cronjobs:view",
        "service": "services:view",
    }
    permission_key = perm_map.get(resource_type, "resources:view")
    if evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key=permission_key,
        resource_type=resource_type,
        resource_name=resource_name,
    ):
        return True

    if resource_type != "pod":
        return False

    for rule in _load_rules(user):
        if rule.effect != "allow":
            continue
        if rule.cluster_id != cluster_id or (rule.namespace or "") != namespace:
            continue
        if (rule.resource_type or "") != "deployment" or not (rule.resource_name or "").strip():
            continue
        if not _pod_matches_deployment_name(resource_name, rule.resource_name.strip()):
            continue
        if evaluate_access(
            user,
            cluster_id=cluster_id,
            namespace=namespace,
            permission_key="deployments:view",
            resource_type="deployment",
            resource_name=rule.resource_name.strip(),
        ):
            return True
    return False


def can_view_logs(
    user: User,
    cluster_id: str,
    namespace: str,
    pod_name: str,
    container_name: Optional[str] = None,
) -> bool:
    if evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="logs:view",
        resource_type="pod",
        resource_name=pod_name,
        container_name=container_name,
    ):
        return True
    if container_name and evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="logs:view",
        resource_type="container",
        resource_name=pod_name,
        container_name=container_name,
    ):
        return True
    return evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="logs:view",
        resource_type="namespace",
    )


def can_view_service_port(
    user: User,
    cluster_id: str,
    namespace: str,
    service_name: str,
    port: int,
) -> bool:
    if not _role_allows_permission(user, "services:ports:view"):
        return False
    return evaluate_access(
        user,
        cluster_id=cluster_id,
        namespace=namespace,
        permission_key="services:ports:view",
        resource_type="service_port",
        resource_name=service_name,
        port=port,
    )


def get_accessible_cluster_ids(user: User, all_cluster_ids: List[str]) -> Set[str]:
    if is_admin(user):
        return set(all_cluster_ids)
    return {cid for cid in all_cluster_ids if can_access_cluster(user, cid)}


def filter_clusters_for_user(user: User, cluster_items: List[dict]) -> List[dict]:
    if is_admin(user):
        return cluster_items
    allowed = get_accessible_cluster_ids(user, [c.get("id") for c in cluster_items if c.get("id")])
    return [item for item in cluster_items if item.get("id") in allowed]


def allowed_namespace_names_for_cluster(user: User, cluster_id: str) -> Optional[Set[str]]:
    """
    Namespace names the user may access for a cluster.
    None means all namespaces in the cluster (full cluster grant).
  """
    if is_admin(user):
        return None
    if not can_access_cluster(user, cluster_id):
        return set()

    rules = _load_rules(user)
    if _uses_legacy_only(user, rules):
        cluster_ns = {ns for cid, ns in _legacy_namespace_pairs(user) if cid == cluster_id}
        return None if not cluster_ns else cluster_ns

    scoped = {
        (rule.namespace or "").strip()
        for rule in rules
        if rule.cluster_id == cluster_id
        and rule.effect == "allow"
        and rule.permission_key in ("namespaces:view", "resources:view")
        and (rule.resource_type or "cluster") in ("namespace", "pod", "deployment", "service")
        and (rule.namespace or "").strip()
    }
    if not scoped:
        return None
    return scoped


def filter_namespaces_for_user(user: User, cluster_id: str, namespaces: List) -> List:
    if is_admin(user):
        return namespaces
    allowed = allowed_namespace_names_for_cluster(user, cluster_id)
    if allowed is None:
        return namespaces
    filtered = []
    for ns in namespaces:
        name = ns.get("name") if isinstance(ns, dict) else ns
        if name and name in allowed:
            filtered.append(ns)
    return filtered


def filter_namespace_resources(user: User, cluster_id: str, resources: Dict[str, Any]) -> Dict[str, Any]:
    if is_admin(user):
        return resources
    namespace = resources.get("namespace") or ""
    pods = []
    for pod in resources.get("pods") or []:
        name = pod.get("name")
        if not name or not can_access_resource(user, cluster_id, namespace, "pod", name):
            continue
        pod_copy = dict(pod)
        actions = list(pod_copy.get("actions") or [])
        if "logs" in actions and not can_view_logs(user, cluster_id, namespace, name):
            actions = [a for a in actions if a != "logs"]
        pod_copy["actions"] = actions
        pod_copy["canViewLogs"] = can_view_logs(user, cluster_id, namespace, name)
        pods.append(pod_copy)

    deployments = [
        d
        for d in resources.get("deployments") or []
        if d.get("name")
        and can_access_resource(user, cluster_id, namespace, "deployment", d["name"])
    ]

    replicasets = []
    for rs in resources.get("replicasets") or []:
        name = rs.get("name")
        if not name:
            continue
        owner = (rs.get("owner") or "").strip()
        if owner and owner != "-" and can_access_resource(user, cluster_id, namespace, "deployment", owner):
            replicasets.append(rs)
        elif can_access_resource(user, cluster_id, namespace, "replicaset", name):
            replicasets.append(rs)

    workload_types = (
        ("statefulsets", "statefulset"),
        ("daemonsets", "daemonset"),
        ("jobs", "job"),
        ("cronjobs", "cronjob"),
    )
    filtered_workloads: Dict[str, List[Dict[str, Any]]] = {}
    for list_key, resource_type in workload_types:
        filtered_workloads[list_key] = [
            item
            for item in resources.get(list_key) or []
            if item.get("name")
            and can_access_resource(user, cluster_id, namespace, resource_type, item["name"])
        ]

    services = []
    for svc in resources.get("services") or []:
        name = svc.get("name")
        if not name or not can_access_resource(user, cluster_id, namespace, "service", name):
            continue
        svc_copy = dict(svc)
        if not _role_allows_permission(user, "services:ports:view"):
            svc_copy["ports"] = []
            svc_copy["portsDetail"] = []
            svc_copy["canViewPorts"] = False
        else:
            ports_raw = svc_copy.get("ports") or []
            if isinstance(ports_raw, str):
                ports_raw = [p.strip() for p in ports_raw.split(",") if p.strip()]
            visible_ports = []
            ports_detail = []
            for p in ports_raw:
                try:
                    port_num = int(p)
                except (TypeError, ValueError):
                    continue
                if can_view_service_port(user, cluster_id, namespace, name, port_num):
                    visible_ports.append(port_num)
                    ports_detail.append({"port": port_num, "allowed": True})
            svc_copy["ports"] = visible_ports
            svc_copy["portsDetail"] = ports_detail
            svc_copy["canViewPorts"] = bool(visible_ports)
        services.append(svc_copy)

    # ConfigMaps and Secrets have no per-resource access rules; they are visible
    # to any non-admin who can reach the namespace. The list payloads never carry
    # secret values (only names/types/labels), so this matches resources:view.
    namespace_visible = can_access_namespace(user, cluster_id, namespace)
    configmaps = (resources.get("configmaps") or []) if namespace_visible else []
    secrets = (resources.get("secrets") or []) if namespace_visible else []

    return {
        "namespace": namespace,
        "pods": pods,
        "deployments": deployments,
        "replicasets": replicasets,
        "statefulsets": filtered_workloads["statefulsets"],
        "daemonsets": filtered_workloads["daemonsets"],
        "jobs": filtered_workloads["jobs"],
        "cronjobs": filtered_workloads["cronjobs"],
        "services": services,
        "configmaps": configmaps,
        "secrets": secrets,
    }


_INVOLVED_KIND_TO_RESOURCE_TYPE = {
    "Pod": "pod",
    "Deployment": "deployment",
    "ReplicaSet": "replicaset",
    "StatefulSet": "statefulset",
    "DaemonSet": "daemonset",
    "Job": "job",
    "CronJob": "cronjob",
    "Service": "service",
}


def filter_namespace_events(
    user: User,
    cluster_id: str,
    namespace: str,
    events_payload: Dict[str, Any],
) -> Dict[str, Any]:
    if is_admin(user):
        return events_payload
    if can_access_namespace(user, cluster_id, namespace):
        rules = _load_rules(user)
        resource_scoped = any(
            rule.cluster_id == cluster_id
            and (rule.namespace or "") == namespace
            and (rule.resource_type or "") in ("pod", "deployment", "service")
            and (rule.resource_name or "").strip()
            for rule in rules
        )
        if not resource_scoped:
            return events_payload
    filtered = []
    for event in events_payload.get("items") or []:
        kind = event.get("involvedKind") or ""
        name = event.get("involvedName") or ""
        if not kind or not name:
            filtered.append(event)
            continue
        resource_type = _INVOLVED_KIND_TO_RESOURCE_TYPE.get(kind)
        if resource_type:
            if can_access_resource(user, cluster_id, namespace, resource_type, name):
                filtered.append(event)
        else:
            filtered.append(event)
    return {
        **events_payload,
        "items": filtered,
        "count": len(filtered),
    }


def can_view_alert(user: User, alert: Dict[str, Any]) -> bool:
    if is_admin(user):
        return True
    if not _role_allows_permission(user, "alerts:view"):
        return False

    cluster_id = alert.get("clusterId") or alert.get("cluster")
    if not cluster_id or not can_access_cluster(user, cluster_id):
        return False

    namespace = (alert.get("namespace") or "").strip() or None
    resource_name = (alert.get("pod") or alert.get("resourceName") or alert.get("resource") or "").strip() or None

    if resource_name and namespace:
        if evaluate_access(
            user,
            cluster_id=cluster_id,
            namespace=namespace,
            permission_key="alerts:view",
            resource_type="pod",
            resource_name=resource_name,
        ):
            return True

    if namespace:
        return evaluate_access(
            user,
            cluster_id=cluster_id,
            namespace=namespace,
            permission_key="alerts:view",
            resource_type="namespace",
        )

    return evaluate_access(
        user,
        cluster_id=cluster_id,
        permission_key="alerts:view",
        resource_type="cluster",
    )


def filter_alerts_for_user(user: User, alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if is_admin(user):
        return alerts
    return [alert for alert in alerts if can_view_alert(user, alert)]
