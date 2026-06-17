from __future__ import annotations

from .db import db
from .mock_data import SETTINGS
from .models import AccessRule, AppSettings, Permission, Role, User, UserClusterAccess, role_permissions as rp_table
from .passwords import hash_password
from .rbac_data import (
    DEFAULT_USERS,
    HERMES_AGENT_PERMISSIONS,
    OPERATOR_PERMISSIONS,
    PERMISSIONS,
    ROLE_DEFINITIONS,
    VIEWER_PERMISSIONS,
)

MOCK_CLUSTER_IDS = frozenset({"prod-us-east", "staging-eu-west"})
DEMO_USER_SYNC = {
    "viewer": VIEWER_PERMISSIONS,
    "operator": OPERATOR_PERMISSIONS,
    "hermes-agent": HERMES_AGENT_PERMISSIONS,
}


def _seed_permissions() -> dict:
    by_key = {}
    for key, description in PERMISSIONS:
        perm = Permission.query.filter_by(key=key).first()
        if not perm:
            perm = Permission(key=key, description=description)
            db.session.add(perm)
        else:
            perm.description = description
        by_key[key] = perm
    db.session.flush()
    return by_key


def _seed_roles(permissions_by_key: dict) -> dict:
    roles_by_name = {}
    for name, definition in ROLE_DEFINITIONS.items():
        role = Role.query.filter_by(name=name).first()
        default_permissions = [
            permissions_by_key[key]
            for key in definition["permissions"]
            if key in permissions_by_key
        ]
        if not role:
            role = Role(
                name=name,
                description=definition["description"],
                is_system_role=definition["is_system_role"],
            )
            db.session.add(role)
            role.permissions = default_permissions
        elif definition.get("is_system_role", False):
            # For system roles, add any missing permissions directly via the table
            rows = db.session.execute(
                db.text("SELECT permission_id FROM role_permissions WHERE role_id = :rid"),
                {"rid": role.id},
            ).fetchall()
            existing_perm_ids = {r[0] for r in rows}
            for key in definition["permissions"]:
                perm = permissions_by_key.get(key)
                if perm and perm.id not in existing_perm_ids:
                    db.session.execute(
                        rp_table.insert().values(role_id=role.id, permission_id=perm.id)
                    )
                    existing_perm_ids.add(perm.id)
        elif not role.permissions:
            role.permissions = default_permissions
        roles_by_name[name] = role
    db.session.flush()
    return roles_by_name


def _seed_users(roles_by_name: dict) -> None:
    for spec in DEFAULT_USERS:
        user = User.query.filter_by(username=spec["username"]).first()
        role = roles_by_name.get(spec["role"])
        if not user:
            user = User(
                username=spec["username"],
                email=spec["email"],
                password_hash=hash_password(spec["password"]),
                full_name=spec["full_name"],
                role=role,
                is_active=True,
            )
            db.session.add(user)
            continue
        # Existing users are left as-is so UI edits survive restarts.
        if role and user.role_id is None:
            user.role = role
        if not user.password_hash:
            user.password_hash = hash_password(spec["password"])


def _seed_role_cluster_access(username: str, cluster_ids: tuple[str, ...]) -> None:
    user = User.query.filter_by(username=username).first()
    if not user or user.role is None or user.role.name != username:
        return
    if UserClusterAccess.query.filter_by(user_id=user.id).count():
        return
    for cluster_id in cluster_ids:
        db.session.add(UserClusterAccess(user_id=user.id, cluster_id=cluster_id, can_view=True))


def _full_cluster_rules(cluster_id: str, permission_keys: list[str]) -> list[dict]:
    return [
        {
            "clusterId": cluster_id,
            "resourceType": "cluster",
            "permissionKey": permission_key,
            "effect": "allow",
        }
        for permission_key in permission_keys
    ]


def _sync_demo_users_to_discovered_clusters() -> None:
    """Grant viewer/operator access to live clusters when they only have mock seed IDs."""
    from .access_rules import apply_access_rules
    from .k8s_provider import list_clusters_from_k8s, should_use_real_k8s

    if not should_use_real_k8s():
        return

    try:
        discovered = [
            item["id"]
            for item in list_clusters_from_k8s().get("items", [])
            if item.get("id")
        ]
    except Exception:
        return

    if not discovered:
        return

    discovered_set = set(discovered)

    for username, permission_keys in DEMO_USER_SYNC.items():
        user = User.query.filter_by(username=username).first()
        if not user or not user.role or user.role.name != username:
            continue

        existing_ids = {
            rule.cluster_id
            for rule in AccessRule.query.filter_by(user_id=user.id).all()
            if rule.cluster_id
        }
        if existing_ids & discovered_set:
            continue
        if existing_ids and not existing_ids <= MOCK_CLUSTER_IDS:
            continue

        rules_payload: list[dict] = []
        for cluster_id in discovered:
            rules_payload.extend(_full_cluster_rules(cluster_id, permission_keys))
        apply_access_rules(user, rules_payload)


_NAMESPACE_RESOURCE_VIEW_KEYS = frozenset(
    {
        "resources:view",
        "pods:view",
        "deployments:view",
        "replicasets:view",
        "statefulsets:view",
        "daemonsets:view",
        "jobs:view",
        "cronjobs:view",
        "services:view",
        "inventory:view",
        "helm:view",
    }
)
_NAMESPACE_BREADTH_SIGNAL_KEYS = frozenset(
    {"overview:view", "alerts:view", "services:ports:view"}
)


def _repair_incomplete_namespace_resource_grants() -> None:
    """Add workload view rules when a namespace grant was saved without view_resources."""
    from .access_engine import is_admin
    from .access_rules import apply_access_rules

    for user in User.query.all():
        if not user.role or is_admin(user):
            continue

        rules = AccessRule.query.filter_by(user_id=user.id).all()
        if not rules:
            continue

        role_keys = {perm.key for perm in user.role.permissions}
        if "resources:view" not in role_keys:
            continue

        by_scope: dict[tuple[str, str], set[str]] = {}
        named_resource_scopes: set[tuple[str, str]] = set()
        for rule in rules:
            if rule.effect != "allow" or not rule.namespace:
                continue
            scope = (rule.cluster_id, rule.namespace)
            if (rule.resource_name or "").strip():
                named_resource_scopes.add(scope)
                continue
            if (rule.resource_type or "cluster") not in ("namespace", "cluster"):
                continue
            by_scope.setdefault(scope, set()).add(rule.permission_key)

        additions: list[dict] = []
        for scope, perms in by_scope.items():
            cluster_id, namespace = scope
            if scope in named_resource_scopes:
                continue
            if "namespaces:view" not in perms:
                continue
            if perms & _NAMESPACE_RESOURCE_VIEW_KEYS:
                continue
            if not perms & _NAMESPACE_BREADTH_SIGNAL_KEYS:
                continue

            for permission_key in sorted(_NAMESPACE_RESOURCE_VIEW_KEYS & role_keys):
                additions.append(
                    {
                        "clusterId": cluster_id,
                        "namespace": namespace,
                        "resourceType": "namespace",
                        "permissionKey": permission_key,
                        "effect": "allow",
                    }
                )

        if not additions:
            continue

        merged = [
            {
                "clusterId": rule.cluster_id,
                "namespace": rule.namespace,
                "resourceType": rule.resource_type or "cluster",
                "resourceName": rule.resource_name,
                "containerName": rule.container_name,
                "port": rule.port,
                "permissionKey": rule.permission_key,
                "effect": rule.effect,
            }
            for rule in rules
        ]
        merged.extend(additions)
        apply_access_rules(user, merged)


def _repair_full_cluster_view_permissions() -> None:
    """Ensure full-cluster grants include every view permission on the user's role."""
    from .access_engine import is_admin
    from .access_rules import apply_access_rules

    view_like_suffixes = (":view",)

    for user in User.query.all():
        if not user.role or is_admin(user):
            continue

        rules = AccessRule.query.filter_by(user_id=user.id).all()
        if not rules:
            continue

        has_named_resource_rules = any(
            (rule.resource_name or "").strip()
            and (rule.resource_type or "") in ("pod", "deployment", "service")
            for rule in rules
        )
        if has_named_resource_rules:
            continue

        role_keys = {perm.key for perm in user.role.permissions}
        cluster_ids = {
            rule.cluster_id
            for rule in rules
            if rule.cluster_id
            and not (rule.namespace or "").strip()
            and (rule.resource_type or "cluster") == "cluster"
            and rule.permission_key == "clusters:view"
        }
        if not cluster_ids:
            continue

        additions: list[dict] = []
        for cluster_id in cluster_ids:
            existing = {
                rule.permission_key
                for rule in rules
                if rule.cluster_id == cluster_id and not (rule.namespace or "").strip()
            }
            for permission_key in sorted(role_keys):
                if permission_key in existing:
                    continue
                if not any(permission_key.endswith(suffix) for suffix in view_like_suffixes):
                    continue
                additions.append(
                    {
                        "clusterId": cluster_id,
                        "resourceType": "cluster",
                        "permissionKey": permission_key,
                        "effect": "allow",
                    }
                )

        if not additions:
            continue

        merged = [
            {
                "clusterId": rule.cluster_id,
                "namespace": rule.namespace,
                "resourceType": rule.resource_type or "cluster",
                "resourceName": rule.resource_name,
                "containerName": rule.container_name,
                "port": rule.port,
                "permissionKey": rule.permission_key,
                "effect": rule.effect,
            }
            for rule in rules
        ]
        merged.extend(additions)
        apply_access_rules(user, merged)


def _infer_deployment_from_pod_name(pod_name: str) -> str:
    parts = (pod_name or "").split("-")
    if len(parts) < 3:
        return ""
    return "-".join(parts[:-2])


def _repair_named_resource_view_permissions() -> None:
    """Add view rules for named resources that only have logs; map stale pod names to deployments."""
    from .access_engine import is_admin
    from .access_rules import apply_access_rules

    view_by_type = {
        "pod": "pods:view",
        "deployment": "deployments:view",
        "service": "services:view",
    }

    for user in User.query.all():
        if not user.role or is_admin(user):
            continue

        rules = AccessRule.query.filter_by(user_id=user.id).all()
        if not rules:
            continue

        role_keys = {perm.key for perm in user.role.permissions}
        existing = {
            (rule.cluster_id, rule.namespace or "", rule.resource_type or "", rule.resource_name or "", rule.permission_key)
            for rule in rules
        }
        additions: list[dict] = []
        for rule in rules:
            resource_type = (rule.resource_type or "").strip()
            resource_name = (rule.resource_name or "").strip()
            if not resource_name or resource_type not in view_by_type:
                continue
            view_key = view_by_type[resource_type]
            if view_key not in role_keys:
                continue
            key = (rule.cluster_id, rule.namespace or "", resource_type, resource_name, view_key)
            if key in existing:
                continue
            additions.append(
                {
                    "clusterId": rule.cluster_id,
                    "namespace": rule.namespace,
                    "resourceType": resource_type,
                    "resourceName": resource_name,
                    "permissionKey": view_key,
                    "effect": "allow",
                }
            )
            if resource_type == "pod" and "deployments:view" in role_keys:
                deployment_name = _infer_deployment_from_pod_name(resource_name)
                if deployment_name:
                    dep_key = (
                        rule.cluster_id,
                        rule.namespace or "",
                        "deployment",
                        deployment_name,
                        "deployments:view",
                    )
                    if dep_key not in existing:
                        additions.append(
                            {
                                "clusterId": rule.cluster_id,
                                "namespace": rule.namespace,
                                "resourceType": "deployment",
                                "resourceName": deployment_name,
                                "permissionKey": "deployments:view",
                                "effect": "allow",
                            }
                        )

        if not additions:
            continue

        merged = [
            {
                "clusterId": rule.cluster_id,
                "namespace": rule.namespace,
                "resourceType": rule.resource_type or "cluster",
                "resourceName": rule.resource_name,
                "containerName": rule.container_name,
                "port": rule.port,
                "permissionKey": rule.permission_key,
                "effect": rule.effect,
            }
            for rule in rules
        ]
        merged.extend(additions)
        apply_access_rules(user, merged)


def seed_defaults() -> None:
    permissions_by_key = _seed_permissions()
    roles_by_name = _seed_roles(permissions_by_key)
    _seed_users(roles_by_name)
    cluster_ids = ("prod-us-east", "staging-eu-west")
    _seed_role_cluster_access("viewer", cluster_ids)
    _seed_role_cluster_access("operator", cluster_ids)
    _seed_role_cluster_access("hermes-agent", cluster_ids)

    import threading
    from flask import current_app as _cur_app
    _flask_app = _cur_app._get_current_object()

    def _bg_k8s_sync() -> None:
        with _flask_app.app_context():
            _sync_demo_users_to_discovered_clusters()

    threading.Thread(target=_bg_k8s_sync, daemon=True, name="seed-k8s-sync").start()

    _repair_incomplete_namespace_resource_grants()
    _repair_full_cluster_view_permissions()
    _repair_named_resource_view_permissions()

    existing_settings = AppSettings.query.first()
    if not existing_settings:
        db.session.add(
            AppSettings(
                theme=SETTINGS.get("theme", "system"),
                refresh_interval_seconds=SETTINGS.get("refreshIntervalSeconds", 30),
                default_cluster=SETTINGS.get("defaultCluster", "prod-us-east"),
                notifications=SETTINGS.get("notifications", {"alerts": True, "upgrades": True}),
            )
        )

    db.session.commit()
