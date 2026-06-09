"""Access rule persistence and legacy sync."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .access_engine import invalidate_access_rules_cache
from .audit import log_audit
from .db import db
from .models import AccessRule, User, UserClusterAccess, UserNamespaceAccess

VALID_RESOURCE_TYPES = {
    "cluster",
    "namespace",
    "pod",
    "deployment",
    "service",
    "container",
    "service_port",
}
VALID_EFFECTS = {"allow", "deny"}


def access_rule_to_dict(rule: AccessRule) -> Dict[str, Any]:
    return {
        "id": rule.id,
        "userId": rule.user_id,
        "clusterId": rule.cluster_id,
        "namespace": rule.namespace,
        "resourceType": rule.resource_type,
        "resourceName": rule.resource_name,
        "containerName": rule.container_name,
        "port": rule.port,
        "permissionKey": rule.permission_key,
        "effect": rule.effect,
        "createdAt": rule.created_at.isoformat() if rule.created_at else None,
        "updatedAt": rule.updated_at.isoformat() if rule.updated_at else None,
    }


def get_user_access_rules(user: User) -> List[Dict[str, Any]]:
    rules = AccessRule.query.filter_by(user_id=user.id).order_by(AccessRule.id.asc()).all()
    return [access_rule_to_dict(r) for r in rules]


def parse_access_rule_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    resource_type = str(item.get("resourceType", "cluster")).strip() or "cluster"
    if resource_type not in VALID_RESOURCE_TYPES:
        raise ValueError(f"Invalid resourceType: {resource_type}")
    effect = str(item.get("effect", "allow")).strip().lower()
    if effect not in VALID_EFFECTS:
        raise ValueError(f"Invalid effect: {effect}")
    cluster_id = str(item.get("clusterId", "")).strip()
    permission_key = str(item.get("permissionKey", "")).strip()
    if not cluster_id or not permission_key:
        raise ValueError("clusterId and permissionKey are required")
    port = item.get("port")
    if port is not None and port != "":
        port = int(port)
    else:
        port = None
    namespace = str(item.get("namespace", "")).strip() or None
    resource_name = str(item.get("resourceName", "")).strip() or None
    container_name = str(item.get("containerName", "")).strip() or None
    return {
        "cluster_id": cluster_id,
        "namespace": namespace,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "container_name": container_name,
        "port": port,
        "permission_key": permission_key,
        "effect": effect,
    }


def apply_access_rules(user: User, rules_payload: Optional[List[Dict[str, Any]]]) -> None:
    AccessRule.query.filter_by(user_id=user.id).delete()
    if not rules_payload:
        UserClusterAccess.query.filter_by(user_id=user.id).delete()
        UserNamespaceAccess.query.filter_by(user_id=user.id).delete()
        return

    parsed_rules = []
    for item in rules_payload:
        if not isinstance(item, dict):
            continue
        parsed_rules.append(parse_access_rule_payload(item))

    for data in parsed_rules:
        db.session.add(AccessRule(user_id=user.id, **data))

    _sync_legacy_from_rules(user, parsed_rules)
    db.session.expire(user, ["access_rules"])
    invalidate_access_rules_cache(user.id)


def _sync_legacy_from_rules(user: User, parsed_rules: List[Dict[str, Any]]) -> None:
    """Keep legacy tables in sync for backward-compatible reads."""
    UserClusterAccess.query.filter_by(user_id=user.id).delete()
    UserNamespaceAccess.query.filter_by(user_id=user.id).delete()

    cluster_ids = set()
    namespace_pairs = set()

    for rule in parsed_rules:
        if rule["effect"] != "allow":
            continue
        if rule["permission_key"] in ("clusters:view", "namespaces:view", "resources:view", "pods:view"):
            cluster_ids.add(rule["cluster_id"])
        if rule["namespace"] and rule["resource_type"] in (
            "namespace",
            "pod",
            "deployment",
            "service",
            "container",
            "service_port",
        ):
            namespace_pairs.add((rule["cluster_id"], rule["namespace"]))

    for cluster_id in cluster_ids:
        user.cluster_access_entries.append(
            UserClusterAccess(user_id=user.id, cluster_id=cluster_id, can_view=True)
        )
    for cluster_id, namespace in namespace_pairs:
        user.namespace_access_entries.append(
            UserNamespaceAccess(
                user_id=user.id,
                cluster_id=cluster_id,
                namespace=namespace,
                can_view=True,
            )
        )


def migrate_legacy_access_to_rules(user: User) -> None:
    if AccessRule.query.filter_by(user_id=user.id).count():
        return
    rules: List[Dict[str, Any]] = []
    for row in user.cluster_access_entries:
        if row.can_view:
            rules.append(
                {
                    "clusterId": row.cluster_id,
                    "resourceType": "cluster",
                    "permissionKey": "clusters:view",
                    "effect": "allow",
                }
            )
    for row in user.namespace_access_entries:
        if row.can_view:
            rules.append(
                {
                    "clusterId": row.cluster_id,
                    "namespace": row.namespace,
                    "resourceType": "namespace",
                    "permissionKey": "namespaces:view",
                    "effect": "allow",
                }
            )
            rules.append(
                {
                    "clusterId": row.cluster_id,
                    "namespace": row.namespace,
                    "resourceType": "namespace",
                    "permissionKey": "resources:view",
                    "effect": "allow",
                }
            )
    if rules:
        apply_access_rules(user, rules)


def migrate_all_users_legacy_rules() -> None:
    for user in User.query.all():
        migrate_legacy_access_to_rules(user)
    db.session.commit()
