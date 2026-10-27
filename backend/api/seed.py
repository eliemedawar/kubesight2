from __future__ import annotations

import logging
import os
from hashlib import sha256

from .db import db
from .mock_data import SETTINGS
from .models import AccessRule, ApiToken, AppSettings, Permission, Role, User, UserClusterAccess
from .passwords import hash_password
from .runtime_config import env_flag
from .rbac_data import (
    AGENT_PLATFORM_PERMISSIONS,
    DEFAULT_USERS,
    HERMES_AGENT_PERMISSIONS,
    SERVICE_ACCOUNT_USERNAMES,
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

logger = logging.getLogger(__name__)

# Accounts with well-known passwords (viewer123, operator123). A deployment
# turns them off with KUBESIGHT_SEED_DEMO_USERS=false; admin and hermes-agent
# are always created.
DEMO_USERNAMES = frozenset({"viewer", "operator"})
# The API token a deployment hands Hermes (KUBESIGHT_HERMES_MCP_TOKEN) is
# registered under this name, so a changed value can retire the old one.
DEPLOYMENT_HERMES_TOKEN_NAME = "Hermes MCP (set by the deployment)"
# Same for the agent platform's token (KUBESIGHT_AGENT_PLATFORM_MCP_TOKEN).
DEPLOYMENT_AGENT_PLATFORM_TOKEN_NAME = "Agent platform MCP (set by the deployment)"


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
            grant_new_role_defaults(role, name, default_permissions)
        elif not role.permissions:
            role.permissions = default_permissions
        record_role_defaults(name, [perm.key for perm in default_permissions])
        roles_by_name[name] = role
    db.session.flush()
    return roles_by_name


def _granted_defaults(role_name: str) -> set:
    from .models import RoleDefaultGrant

    return {
        row.permission_key
        for row in RoleDefaultGrant.query.filter_by(role_name=role_name).all()
    }


def record_role_defaults(role_name: str, keys) -> None:
    """Remember that these built-in defaults have been handed to ``role_name``."""
    from .models import RoleDefaultGrant

    already = _granted_defaults(role_name)
    for key in dict.fromkeys(keys):
        if key not in already:
            db.session.add(RoleDefaultGrant(role_name=role_name, permission_key=key))
            already.add(key)


def grant_new_role_defaults(role: Role, role_name: str, default_permissions) -> None:
    """Top a built-in role up with defaults it has NEVER been granted before.

    A default an admin removed stays removed: it is in the ledger, so it is not
    forced back on the next restart. A role with no ledger yet (an installation
    from before the ledger existed) gets the previous top-up once — exactly what
    every earlier boot already did — and is recorded from then on. The caller
    records the full default list afterwards.

    Appends through the ORM relationship (never a direct join-table insert): a
    stale loaded collection would let the later exact Hermes-role
    reconciliation enqueue the same pair again and violate the composite unique
    constraint.
    """
    granted = _granted_defaults(role_name)
    existing_perm_ids = {permission.id for permission in role.permissions}
    for permission in default_permissions:
        if granted and permission.key in granted:
            continue
        if permission.id not in existing_perm_ids:
            role.permissions.append(permission)
            existing_perm_ids.add(permission.id)


def _seed_users(roles_by_name: dict) -> None:
    # Both only matter the first time, when the user is created: an existing
    # admin keeps the password it has.
    initial_admin_password = os.getenv("KUBESIGHT_INITIAL_ADMIN_PASSWORD", "").strip()
    seed_demo_users = env_flag("KUBESIGHT_SEED_DEMO_USERS", True)
    for spec in DEFAULT_USERS:
        user = User.query.filter_by(username=spec["username"]).first()
        role = roles_by_name.get(spec["role"])
        if not user:
            if spec["username"] in DEMO_USERNAMES and not seed_demo_users:
                continue
            password = spec["password"]
            first_login = False
            if spec["username"] == "admin" and initial_admin_password:
                # Treated like a temporary password: the first sign-in must
                # replace it and enrol MFA before anything else opens.
                password = initial_admin_password
                first_login = True
            user = User(
                username=spec["username"],
                email=spec["email"],
                password_hash=hash_password(password),
                full_name=spec["full_name"],
                role=role,
                is_active=True,
                is_service_account=spec["username"] in SERVICE_ACCOUNT_USERNAMES,
                interactive_login_enabled=spec["username"] not in SERVICE_ACCOUNT_USERNAMES,
                must_change_password=first_login,
                first_login_completed=not first_login,
            )
            db.session.add(user)
            continue
        # Existing users are left as-is so UI edits survive restarts.
        if role and user.role_id is None:
            user.role = role
        if not user.password_hash:
            user.password_hash = hash_password(spec["password"])
        if spec["username"] in SERVICE_ACCOUNT_USERNAMES:
            user.role = role
            user.is_service_account = True
            user.interactive_login_enabled = False


def _seed_deployment_token(owner: User | None, env_name: str, token_name: str) -> None:
    """Register the ``ksa_`` token the deployment gave an agent for the MCP server.

    Lets the Helm chart connect an agent without anyone minting a token in the
    UI. Only the hash is stored, as for any API token. A value that is already
    registered is left as it is, so revoking it in the UI sticks across
    restarts; a new value retires the one the deployment set before.
    """
    raw = os.getenv(env_name, "").strip()
    if not raw or owner is None:
        return
    if not raw.startswith("ksa_") or len(raw) < 24:
        logger.warning("%s ignored: it must start with ksa_ and be at least 24 characters", env_name)
        return
    digest = sha256(raw.encode()).hexdigest()
    existing = ApiToken.query.filter_by(token_hash=digest).first()
    if existing is not None:
        if existing.user_id != owner.id:
            logger.warning("%s ignored: that token already belongs to another user", env_name)
        return
    for old in ApiToken.query.filter_by(user_id=owner.id, name=token_name, is_active=True):
        old.is_active = False
    db.session.add(
        ApiToken(
            user_id=owner.id,
            name=token_name,
            token_prefix=raw[:12],
            token_hash=digest,
            is_active=True,
        )
    )


def _seed_hermes_mcp_token(hermes: User | None) -> None:
    _seed_deployment_token(hermes, "KUBESIGHT_HERMES_MCP_TOKEN", DEPLOYMENT_HERMES_TOKEN_NAME)


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
    # Hermes holds exactly HERMES_AGENT_PERMISSIONS (ticket agent + analysis executor). It deliberately receives neither runtime
    # cluster grants nor any interactive session capability.
    hermes = User.query.filter_by(username="hermes-agent").first()
    if hermes and hermes.role:
        allowed = set(HERMES_AGENT_PERMISSIONS)
        hermes.role.permissions = [
            permission for permission in permissions_by_key.values() if permission.key in allowed
        ]
        AccessRule.query.filter_by(user_id=hermes.id).delete()
        UserClusterAccess.query.filter_by(user_id=hermes.id).delete()
    _seed_hermes_mcp_token(hermes)
    # The agent platform's account keeps exactly its role too. Unlike Hermes,
    # the clusters an admin grants it are kept: its ticket agent reads them
    # when it troubleshoots.
    platform_user = User.query.filter_by(username="agent-platform").first()
    if platform_user and platform_user.role:
        allowed = set(AGENT_PLATFORM_PERMISSIONS)
        platform_user.role.permissions = [
            permission for permission in permissions_by_key.values() if permission.key in allowed
        ]
    _seed_deployment_token(platform_user, "KUBESIGHT_AGENT_PLATFORM_MCP_TOKEN",
                           DEPLOYMENT_AGENT_PLATFORM_TOKEN_NAME)

    import threading
    from flask import current_app as _cur_app
    _flask_app = _cur_app._get_current_object()

    def _bg_k8s_sync() -> None:
        with _flask_app.app_context():
            _sync_demo_users_to_discovered_clusters()

    # Skip the background sync under tests: it is a no-op in mock mode and its
    # app-context teardown races with the in-memory SQLite connection, rolling
    # back the just-seeded rows before this function commits.
    if not _flask_app.config.get("TESTING"):
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
