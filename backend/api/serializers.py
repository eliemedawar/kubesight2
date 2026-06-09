from __future__ import annotations

from typing import Any, Dict, List, Optional

from .access import get_user_cluster_ids, get_user_namespace_access, get_user_permission_keys, is_admin
from .access_rules import access_rule_to_dict, apply_access_rules, get_user_access_rules
from .models import AuditLog, Role, User, UserClusterAccess, UserNamespaceAccess
from .rbac_data import ALL_PERMISSION_KEYS


def role_has_full_access(role: Role) -> bool:
    granted = {perm.key for perm in role.permissions}
    return set(ALL_PERMISSION_KEYS).issubset(granted)


def role_to_dict(role: Role, *, user_count: Optional[int] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "isSystemRole": role.is_system_role,
        "hasFullAccess": role_has_full_access(role),
        "permissions": [perm.key for perm in role.permissions],
    }
    if user_count is not None:
        payload["userCount"] = user_count
    return payload


def user_to_dict(user: User, include_access: bool = False) -> Dict[str, Any]:
    role_name = user.role.name if user.role else None
    permissions = sorted(get_user_permission_keys(user))
    payload: Dict[str, Any] = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "fullName": user.full_name,
        "role": role_name,
        "roleId": user.role_id,
        "isActive": user.is_active,
        "isAdmin": is_admin(user),
        "hasFullAccess": is_admin(user),
        "permissions": permissions,
        "createdAt": user.created_at.isoformat() if user.created_at else None,
        "updatedAt": user.updated_at.isoformat() if user.updated_at else None,
        "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
    }
    if include_access:
        payload["isAdmin"] = is_admin(user)
        payload["clusterAccess"] = get_user_cluster_ids(user)
        payload["namespaceAccess"] = get_user_namespace_access(user)
        payload["accessRules"] = get_user_access_rules(user)
    return payload


def user_list_item(user: User) -> Dict[str, Any]:
    payload = user_to_dict(user, include_access=False)
    payload["clusterAccess"] = [] if is_admin(user) else get_user_cluster_ids(user)
    return payload


def audit_log_to_dict(entry: AuditLog) -> Dict[str, Any]:
    actor_name = None
    if entry.actor:
        actor_name = entry.actor.username
    return {
        "id": entry.id,
        "actorUserId": entry.actor_user_id,
        "actorUsername": actor_name,
        "action": entry.action,
        "targetType": entry.target_type,
        "targetId": entry.target_id,
        "details": entry.details or {},
        "createdAt": entry.created_at.isoformat() if entry.created_at else None,
    }


def parse_cluster_access_payload(cluster_access: Optional[List]) -> List[str]:
    if not cluster_access:
        return []
    return [str(item).strip() for item in cluster_access if str(item).strip()]


def parse_namespace_access_payload(namespace_access: Optional[List]) -> List[Dict[str, str]]:
    if not namespace_access:
        return []
    parsed = []
    for item in namespace_access:
        if not isinstance(item, dict):
            continue
        cluster_id = str(item.get("clusterId", "")).strip()
        namespace = str(item.get("namespace", "")).strip()
        if cluster_id and namespace:
            parsed.append({"clusterId": cluster_id, "namespace": namespace})
    return parsed


def apply_user_access(
    user: User,
    cluster_ids: List[str],
    namespace_rows: List[Dict[str, str]],
    access_rules: Optional[List[Dict[str, Any]]] = None,
) -> None:
    if access_rules is not None:
        apply_access_rules(user, access_rules)
        return
    rules = []
    for cluster_id in cluster_ids:
        rules.append(
            {
                "clusterId": cluster_id,
                "resourceType": "cluster",
                "permissionKey": "clusters:view",
                "effect": "allow",
            }
        )
    for row in namespace_rows:
        rules.append(
            {
                "clusterId": row["clusterId"],
                "namespace": row["namespace"],
                "resourceType": "namespace",
                "permissionKey": "namespaces:view",
                "effect": "allow",
            }
        )
        rules.append(
            {
                "clusterId": row["clusterId"],
                "namespace": row["namespace"],
                "resourceType": "namespace",
                "permissionKey": "resources:view",
                "effect": "allow",
            }
        )
    apply_access_rules(user, rules)
