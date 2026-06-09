"""Access control facade — delegates to access_engine."""

from __future__ import annotations

from typing import Dict, List, Set

from .access_engine import (
    can_access_cluster,
    can_access_namespace,
    can_access_resource,
    can_view_alert,
    can_view_logs,
    can_view_service_port,
    evaluate_access,
    filter_alerts_for_user,
    filter_clusters_for_user,
    filter_namespace_resources,
    filter_namespaces_for_user,
    get_user_permission_keys,
    is_admin,
    user_has_permission,
)

__all__ = [
    "is_admin",
    "get_user_permission_keys",
    "user_has_permission",
    "get_user_cluster_ids",
    "get_user_namespace_access",
    "user_has_cluster_access",
    "user_has_namespace_access",
    "filter_clusters_for_user",
    "filter_namespaces_for_user",
    "filter_namespace_resources",
    "can_access_cluster",
    "can_access_namespace",
    "can_access_resource",
    "can_view_logs",
    "can_view_alert",
    "filter_alerts_for_user",
    "can_view_service_port",
    "evaluate_access",
]


def user_has_cluster_access(user, cluster_id: str) -> bool:
    return can_access_cluster(user, cluster_id)


def user_has_namespace_access(user, cluster_id: str, namespace: str) -> bool:
    return can_access_namespace(user, cluster_id, namespace)


def get_user_cluster_ids(user) -> List[str]:
    if is_admin(user):
        return []
    from .access_engine import _load_rules

    rules = _load_rules(user)
    if rules:
        return sorted(
            {
                r.cluster_id
                for r in rules
                if r.effect == "allow" and r.permission_key in ("clusters:view", "namespaces:view", "resources:view")
            }
        )
    return [row.cluster_id for row in user.cluster_access_entries if row.can_view]


def get_user_namespace_access(user) -> List[Dict[str, str]]:
    if is_admin(user):
        return []
    from .access_engine import _load_rules

    rules = _load_rules(user)
    if rules:
        pairs = {
            (r.cluster_id, r.namespace)
            for r in rules
            if r.effect == "allow"
            and r.namespace
            and r.resource_type in ("namespace", "pod", "deployment", "service", "container", "service_port")
        }
        return [{"clusterId": cid, "namespace": ns} for cid, ns in sorted(pairs)]
    return [
        {"clusterId": row.cluster_id, "namespace": row.namespace}
        for row in user.namespace_access_entries
        if row.can_view
    ]
