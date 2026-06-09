"""Build effective access summaries from user access rules (matches admin UI preview)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from .access import get_user_cluster_ids, get_user_namespace_access
from .access_engine import can_access_cluster, is_admin, user_has_permission
from .access_rules import get_user_access_rules
from .models import User

ALLOWED_ACTIONS: List[Dict[str, Any]] = [
    {
        "id": "view_resources",
        "label": "View Resources",
        "permissions": [
            "resources:view",
            "pods:view",
            "deployments:view",
            "replicasets:view",
            "statefulsets:view",
            "daemonsets:view",
            "jobs:view",
            "cronjobs:view",
            "services:view",
            "namespaces:view",
        ],
    },
    {"id": "view_logs", "label": "View Logs", "permissions": ["logs:view"]},
    {
        "id": "view_metrics",
        "label": "View Metrics",
        "permissions": ["overview:view", "resources:view"],
    },
    {"id": "view_alerts", "label": "View Alerts", "permissions": ["alerts:view"]},
    {
        "id": "upgrade_precheck",
        "label": "Run Upgrade Precheck",
        "permissions": ["upgrades:precheck"],
    },
    {
        "id": "view_service_ports",
        "label": "View Service Ports",
        "permissions": ["services:ports:view", "services:view"],
    },
]

FULL_CLUSTER_ACTION_IDS = [
    "view_resources",
    "view_logs",
    "view_metrics",
    "view_alerts",
    "view_service_ports",
    "upgrade_precheck",
]

NAMESPACE_DEFAULT_ACTION_IDS = [
    "view_resources",
    "view_logs",
    "view_metrics",
    "view_alerts",
    "view_service_ports",
]

DEFAULT_ALLOWED_ACTIONS = ["view_resources", "view_logs"]

ACTION_BY_ID = {action["id"]: action for action in ALLOWED_ACTIONS}


def _role_permissions(user: User) -> Set[str]:
    if not user.role:
        return set()
    return {perm.key for perm in user.role.permissions}


def _filter_action_ids_for_role_user(user: User, action_ids: List[str]) -> List[str]:
    if is_admin(user):
        return list(action_ids)
    role_permissions = _role_permissions(user)
    selectable = {
        action["id"]
        for action in ALLOWED_ACTIONS
        if all(perm in role_permissions for perm in action["permissions"])
    }
    return [action_id for action_id in action_ids if action_id in selectable]


def _empty_cluster_grant(cluster_id: str) -> Dict[str, Any]:
    return {
        "clusterId": cluster_id,
        "allowed": False,
        "mode": "full",
        "namespaces": [],
        "resourceAccess": {
            "namespaces": {},
            "allowedActions": list(DEFAULT_ALLOWED_ACTIONS),
        },
    }


def _sync_allowed_actions_from_rules(grant: Dict[str, Any], cluster_rules: List[Dict[str, Any]]) -> None:
    action_ids: Set[str] = set()
    for rule in cluster_rules:
        for action in ALLOWED_ACTIONS:
            if rule.get("permissionKey") in action["permissions"]:
                action_ids.add(action["id"])
    if action_ids:
        grant["resourceAccess"]["allowedActions"] = sorted(action_ids)


def _parse_cluster_grant(cluster_id: str, allow_rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    grant = _empty_cluster_grant(cluster_id)
    cluster_rules = [rule for rule in allow_rules if rule.get("clusterId") == cluster_id]
    if not cluster_rules:
        return grant

    grant["allowed"] = True

    named_resource_rules = [
        rule
        for rule in cluster_rules
        if rule.get("resourceType") in {"pod", "deployment", "service"}
        and rule.get("resourceName")
        and rule.get("namespace")
    ]
    if named_resource_rules:
        grant["mode"] = "resources"
        for rule in named_resource_rules:
            namespace = rule["namespace"]
            ns_bucket = grant["resourceAccess"]["namespaces"].setdefault(
                namespace, {"pods": [], "deployments": [], "services": []}
            )
            list_key = {
                "pod": "pods",
                "deployment": "deployments",
                "service": "services",
            }[rule["resourceType"]]
            name = rule["resourceName"]
            if name not in ns_bucket[list_key]:
                ns_bucket[list_key].append(name)
        _sync_allowed_actions_from_rules(grant, cluster_rules)
        return grant

    namespace_rules = [rule for rule in cluster_rules if rule.get("namespace")]
    if namespace_rules:
        namespaces = sorted({rule["namespace"] for rule in namespace_rules if rule.get("namespace")})
        namespace_scoped_only = all(
            rule.get("resourceType") == "namespace" or not rule.get("resourceName")
            for rule in namespace_rules
        )
        if namespace_scoped_only and namespaces:
            grant["mode"] = "namespaces"
            grant["namespaces"] = namespaces
            _sync_allowed_actions_from_rules(grant, cluster_rules)
            return grant

    if any(not rule.get("namespace") and rule.get("permissionKey") == "clusters:view" for rule in cluster_rules):
        grant["mode"] = "full"
        _sync_allowed_actions_from_rules(grant, cluster_rules)

    return grant


def _access_rules_to_grants(access_rules: List[Dict[str, Any]], cluster_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    grants = {cluster_id: _empty_cluster_grant(cluster_id) for cluster_id in cluster_ids}
    allow_rules = [rule for rule in access_rules if rule.get("effect") != "deny"]
    touched_ids = sorted({rule["clusterId"] for rule in allow_rules if rule.get("clusterId")})
    for cluster_id in touched_ids:
        grants[cluster_id] = _parse_cluster_grant(cluster_id, allow_rules)
    return grants


def _legacy_grants_from_user(user: User) -> Dict[str, Dict[str, Any]]:
    cluster_ids = get_user_cluster_ids(user)
    namespace_rows = get_user_namespace_access(user)
    grants: Dict[str, Dict[str, Any]] = {}

    for cluster_id in cluster_ids:
        grant = _empty_cluster_grant(cluster_id)
        grant["allowed"] = True
        namespaces = sorted(
            row["namespace"] for row in namespace_rows if row.get("clusterId") == cluster_id and row.get("namespace")
        )
        if namespaces:
            grant["mode"] = "namespaces"
            grant["namespaces"] = namespaces
            grant["resourceAccess"]["allowedActions"] = list(NAMESPACE_DEFAULT_ACTION_IDS)
        else:
            grant["mode"] = "full"
            grant["resourceAccess"]["allowedActions"] = list(FULL_CLUSTER_ACTION_IDS)
        grants[cluster_id] = grant

    return grants


def _count_selected_resources(resource_access: Dict[str, Any]) -> Dict[str, int]:
    counts = {"pods": 0, "deployments": 0, "services": 0, "total": 0}
    for bucket in (resource_access or {}).get("namespaces", {}).values():
        counts["pods"] += len(bucket.get("pods") or [])
        counts["deployments"] += len(bucket.get("deployments") or [])
        counts["services"] += len(bucket.get("services") or [])
    counts["total"] = counts["pods"] + counts["deployments"] + counts["services"]
    return counts


def _namespace_bucket_has_resources(bucket: Dict[str, Any]) -> bool:
    return (
        len(bucket.get("pods") or [])
        + len(bucket.get("deployments") or [])
        + len(bucket.get("services") or [])
    ) > 0


def _effective_action_ids_for_grant(grant: Dict[str, Any], user: User) -> List[str]:
    if grant.get("allowed") is not True:
        return []

    mode = grant.get("mode")
    if mode == "full":
        return _filter_action_ids_for_role_user(user, FULL_CLUSTER_ACTION_IDS)

    if mode == "namespaces":
        stored = grant.get("resourceAccess", {}).get("allowedActions") or []
        action_ids = stored if stored else NAMESPACE_DEFAULT_ACTION_IDS
        return _filter_action_ids_for_role_user(user, action_ids)

    if mode == "resources":
        counts = _count_selected_resources(grant.get("resourceAccess") or {})
        if counts["total"] <= 0:
            return []
        stored = grant.get("resourceAccess", {}).get("allowedActions") or DEFAULT_ALLOWED_ACTIONS
        return _filter_action_ids_for_role_user(user, stored)

    return []


def build_effective_access_summary(
    user: User,
    *,
    cluster_labels: Optional[Dict[str, str]] = None,
    focus_cluster_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Mirror frontend buildEffectiveAccessPreview() using persisted access rules."""
    labels = cluster_labels or {}

    if is_admin(user):
        cluster_lines = ["All clusters (full access)"]
        admin_actions = [
            {"id": action["id"], "label": action["label"]} for action in ALLOWED_ACTIONS
        ]
        return {
            "clusters": cluster_lines,
            "namespaces": [],
            "resources": [],
            "permissions": admin_actions,
            "counts": {"pods": 0, "deployments": 0, "services": 0, "total": 0},
            "hasAccessibleScope": True,
        }

    access_rules = get_user_access_rules(user)
    if access_rules:
        cluster_ids = sorted({rule.get("clusterId") for rule in access_rules if rule.get("clusterId")})
        grants = _access_rules_to_grants(access_rules, cluster_ids)
    else:
        grants = _legacy_grants_from_user(user)
        cluster_ids = sorted(grants.keys())

    if focus_cluster_id and focus_cluster_id not in grants:
        grants[focus_cluster_id] = _empty_cluster_grant(focus_cluster_id)

    cluster_lines: List[str] = []
    namespaces: List[str] = []
    resources: List[str] = []
    action_id_set: Set[str] = set()
    counts = {"pods": 0, "deployments": 0, "services": 0, "total": 0}

    for grant in grants.values():
        if grant.get("allowed") is not True:
            continue

        for action_id in _effective_action_ids_for_grant(grant, user):
            action_id_set.add(action_id)

        cluster_id = grant["clusterId"]
        label = labels.get(cluster_id) or cluster_id

        if grant.get("mode") == "full":
            cluster_lines.append(f"{label} (all namespaces)")
            continue

        cluster_lines.append(label)

        if grant.get("mode") == "namespaces":
            namespaces.extend(grant.get("namespaces") or [])

        if grant.get("mode") == "resources":
            for ns, bucket in (grant.get("resourceAccess") or {}).get("namespaces", {}).items():
                if not _namespace_bucket_has_resources(bucket):
                    continue
                namespaces.append(ns)
                for name in bucket.get("pods") or []:
                    resources.append(name)
                for name in bucket.get("deployments") or []:
                    resources.append(f"{name} (deployment)")
                for name in bucket.get("services") or []:
                    resources.append(f"{name} (service)")

            resource_counts = _count_selected_resources(grant.get("resourceAccess") or {})
            counts["pods"] += resource_counts["pods"]
            counts["deployments"] += resource_counts["deployments"]
            counts["services"] += resource_counts["services"]
            counts["total"] += resource_counts["total"]

    permissions = [
        {"id": action_id, "label": ACTION_BY_ID[action_id]["label"]}
        for action_id in sorted(action_id_set)
        if action_id in ACTION_BY_ID
    ]

    has_scope = bool(cluster_lines) or (
        focus_cluster_id is not None and can_access_cluster(user, focus_cluster_id)
    )

    return {
        "clusters": list(dict.fromkeys(cluster_lines)),
        "namespaces": list(dict.fromkeys(namespaces)),
        "resources": list(dict.fromkeys(resources)),
        "permissions": permissions,
        "counts": counts,
        "hasAccessibleScope": has_scope,
    }
