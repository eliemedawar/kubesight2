"""Permission keys and default role-to-permission mappings."""

PERMISSIONS = [
    ("users:view", "View users"),
    ("users:manage", "Manage users and roles"),
    ("users:create", "Create users"),
    ("users:update", "Update users"),
    ("users:disable", "Disable users"),
    ("users:delete", "Permanently delete users"),
    ("roles:view", "View roles and permissions"),
    ("roles:manage", "Manage role permissions"),
    ("clusters:view", "View clusters"),
    ("clusters:add", "Add clusters"),
    ("clusters:update", "Update clusters"),
    ("clusters:remove", "Remove clusters"),
    ("clusters:test", "Test cluster connections"),
    ("overview:view", "View cluster overview"),
    ("namespaces:view", "View namespaces"),
    ("resources:view", "View namespace resources"),
    ("pods:view", "View pods"),
    ("deployments:view", "View deployments"),
    ("replicasets:view", "View ReplicaSets"),
    ("statefulsets:view", "View StatefulSets"),
    ("daemonsets:view", "View DaemonSets"),
    ("jobs:view", "View Jobs"),
    ("cronjobs:view", "View CronJobs"),
    ("logs:view", "View pod logs"),
    ("alerts:view", "View alerts"),
    ("alerts:manage", "Manage alerts and notifications"),
    ("upgrades:precheck", "Run upgrade prechecks"),
    ("upgrades:start", "Start upgrade workflows"),
    ("settings:view", "View settings"),
    ("settings:manage", "Manage settings"),
    ("audit:view", "View audit logs"),
    ("services:view", "View services"),
    ("services:ports:view", "View service ports"),
    ("inventory:view", "View application inventory"),
    ("inventory:register", "Register existing applications in inventory"),
    ("inventory:update", "Update application catalog metadata"),
    ("inventory:remove", "Remove applications from inventory metadata"),
    ("apps:deploy", "Deploy applications to clusters"),
    ("apps:dryrun", "Run deployment dry-run validation"),
    ("apps:diff", "View deployment diffs"),
    ("apps:delete", "Delete applications from clusters"),
    ("helm:view", "View Helm releases"),
    ("helm:install", "Install Helm releases"),
    ("helm:upgrade", "Upgrade Helm releases"),
    ("helm:rollback", "Rollback Helm releases"),
    ("helm:uninstall", "Uninstall Helm releases"),
    ("helm:values:view", "View Helm release values"),
    ("helm:values:update", "Update Helm release values"),
    ("app_services:view", "View application services"),
    ("app_services:create", "Create application services"),
    ("app_services:update", "Update application services"),
    ("app_services:delete", "Delete application services"),
    ("clients:view", "View clients"),
    ("clients:create", "Create clients"),
    ("clients:update", "Update clients"),
    ("clients:delete", "Delete clients"),
    ("service_blueprints:view", "View service blueprints"),
    ("service_blueprints:create", "Create service blueprints"),
    ("service_blueprints:update", "Edit service blueprints"),
    ("service_blueprints:delete", "Delete service blueprints"),
    ("service_blueprints:deploy", "Deploy from a service blueprint"),
    ("api_tokens:manage", "Create and revoke API tokens"),
    ("deployment_requests:request", "Request a cluster deployment or change"),
    ("deployment_requests:view", "View deployment requests"),
    ("deployment_requests:manage", "Approve or decline deployment requests"),
    ("change_bundles:create", "Create and submit change bundles"),
    ("change_bundles:view", "View change bundles"),
    ("change_bundles:manage", "Approve or reject change bundles"),
    ("components:view", "View topology components"),
    ("components:create", "Create topology components"),
    ("components:update", "Update topology components"),
    ("components:delete", "Delete topology components"),
    ("components:check", "Run topology component health checks"),
]

ALL_PERMISSION_KEYS = [key for key, _ in PERMISSIONS]

# ---------------------------------------------------------------------------
# Permission catalog metadata (single source of truth for the Roles editor UI).
#
# Groups are ordered and drive the grouped permission checkboxes. Any permission
# key that is NOT listed in a group is automatically surfaced under "Other" by
# build_permission_catalog(), so a newly-added permission can never be hidden
# from the UI even if someone forgets to slot it into a group here.
# ---------------------------------------------------------------------------

PERMISSION_GROUPS = [
    {"id": "dashboard", "label": "Dashboard", "keys": ["overview:view"]},
    {
        "id": "clusters",
        "label": "Clusters",
        "keys": ["clusters:view", "clusters:add", "clusters:update", "clusters:remove", "clusters:test"],
    },
    {"id": "namespaces", "label": "Namespaces", "keys": ["namespaces:view"]},
    {
        "id": "resources",
        "label": "Resources",
        "keys": [
            "resources:view", "pods:view", "deployments:view", "replicasets:view",
            "statefulsets:view", "daemonsets:view", "jobs:view", "cronjobs:view",
            "services:view", "services:ports:view",
        ],
    },
    {"id": "logs", "label": "Logs", "keys": ["logs:view"]},
    {"id": "alerts", "label": "Alerts", "keys": ["alerts:view", "alerts:manage"]},
    {
        "id": "inventory",
        "label": "Inventory & Deployments",
        "keys": [
            "inventory:view", "inventory:register", "inventory:update", "inventory:remove",
            "apps:deploy", "apps:dryrun", "apps:diff", "apps:delete",
            "helm:view", "helm:install", "helm:upgrade", "helm:rollback", "helm:uninstall",
            "helm:values:view", "helm:values:update",
        ],
    },
    {
        "id": "changeManagement",
        "label": "Change Management",
        "keys": [
            "deployment_requests:request", "deployment_requests:view", "deployment_requests:manage",
            "change_bundles:create", "change_bundles:view", "change_bundles:manage",
        ],
    },
    {"id": "upgrades", "label": "Upgrade Center", "keys": ["upgrades:precheck", "upgrades:start"]},
    {
        "id": "appServices",
        "label": "Application Services",
        "keys": ["app_services:view", "app_services:create", "app_services:update", "app_services:delete"],
    },
    {
        "id": "clients",
        "label": "Clients",
        "keys": ["clients:view", "clients:create", "clients:update", "clients:delete"],
    },
    {
        "id": "serviceCatalog",
        "label": "Service Catalog",
        "keys": [
            "service_blueprints:view", "service_blueprints:create",
            "service_blueprints:update", "service_blueprints:delete",
            "service_blueprints:deploy",
        ],
    },
    {
        "id": "components",
        "label": "Components",
        "keys": [
            "components:view", "components:create", "components:update",
            "components:delete", "components:check",
        ],
    },
    {
        "id": "administration",
        "label": "Administration",
        "keys": [
            "users:view", "users:manage", "users:create", "users:update", "users:disable", "users:delete",
            "roles:view", "roles:manage", "settings:view", "settings:manage",
            "audit:view", "api_tokens:manage",
        ],
    },
]

# Permissions that grant write/destructive or privilege-escalating power. Surfaced
# to the UI so it can flag them; not an enforcement mechanism on its own.
DANGEROUS_PERMISSION_KEYS = {
    "users:manage", "users:create", "users:update", "users:disable", "users:delete",
    "roles:manage", "clusters:add", "clusters:update", "clusters:remove",
    "settings:manage", "upgrades:start", "apps:deploy", "apps:delete", "inventory:remove",
    "helm:install", "helm:upgrade", "helm:rollback", "helm:uninstall", "helm:values:update",
    "app_services:delete", "clients:delete", "api_tokens:manage",
    "deployment_requests:manage", "change_bundles:manage",
    "service_blueprints:delete", "service_blueprints:deploy",
    "components:delete",
}


def build_permission_catalog():
    """Grouped, labelled, risk-tagged catalog of every permission.

    Returns ``{"groups": [{id,label,keys}], "items": [{key,description,dangerous}]}``.
    Keys not assigned to any group fall into a trailing "Other" group so nothing
    is ever hidden from the Roles editor.
    """
    descriptions = dict(PERMISSIONS)
    grouped: set = set()
    groups = []
    for group in PERMISSION_GROUPS:
        keys = [key for key in group["keys"] if key in descriptions]
        grouped.update(keys)
        if keys:
            groups.append({"id": group["id"], "label": group["label"], "keys": keys})
    leftover = [key for key in ALL_PERMISSION_KEYS if key not in grouped]
    if leftover:
        groups.append({"id": "other", "label": "Other", "keys": leftover})
    items = [
        {"key": key, "description": descriptions.get(key, ""), "dangerous": key in DANGEROUS_PERMISSION_KEYS}
        for key in ALL_PERMISSION_KEYS
    ]
    return {"groups": groups, "items": items}

INVENTORY_VIEW_ALIASES = ("inventory:view", "resources:view")

WORKLOAD_VIEW_PERMISSIONS = [
    "replicasets:view",
    "statefulsets:view",
    "daemonsets:view",
    "jobs:view",
    "cronjobs:view",
]

VIEWER_PERMISSIONS = [
    "clusters:view",
    "overview:view",
    "namespaces:view",
    "resources:view",
    "inventory:view",
    "pods:view",
    "deployments:view",
    *WORKLOAD_VIEW_PERMISSIONS,
    "logs:view",
    "alerts:view",
    "services:view",
    "services:ports:view",
    "helm:view",
    "app_services:view",
    "clients:view",
    "service_blueprints:view",
    "deployment_requests:request",
    "change_bundles:create",
    "change_bundles:view",
    "components:view",
]

OPERATOR_PERMISSIONS = [
    "clusters:view",
    "overview:view",
    "namespaces:view",
    "resources:view",
    "inventory:view",
    "inventory:register",
    "apps:dryrun",
    "apps:diff",
    "helm:view",
    "pods:view",
    "deployments:view",
    *WORKLOAD_VIEW_PERMISSIONS,
    "logs:view",
    "alerts:view",
    "alerts:manage",
    "upgrades:precheck",
    "services:view",
    "services:ports:view",
    "app_services:view",
    "app_services:create",
    "app_services:update",
    "clients:view",
    "clients:create",
    "clients:update",
    "service_blueprints:view",
    "service_blueprints:create",
    "service_blueprints:update",
    "service_blueprints:deploy",
    "deployment_requests:request",
    "deployment_requests:view",
    "change_bundles:create",
    "change_bundles:view",
    "components:view",
    "components:create",
    "components:update",
    "components:check",
]

CLUSTER_ADMIN_PERMISSIONS = [
    "clusters:view",
    "overview:view",
    "namespaces:view",
    "resources:view",
    "inventory:view",
    "inventory:register",
    "inventory:update",
    "inventory:remove",
    "apps:deploy",
    "apps:dryrun",
    "apps:diff",
    "helm:view",
    "helm:install",
    "helm:upgrade",
    "helm:rollback",
    "helm:values:view",
    "helm:values:update",
    "pods:view",
    "deployments:view",
    *WORKLOAD_VIEW_PERMISSIONS,
    "logs:view",
    "alerts:view",
    "alerts:manage",
    "upgrades:precheck",
    "services:view",
    "services:ports:view",
    "app_services:view",
    "app_services:create",
    "app_services:update",
    "app_services:delete",
    "clients:view",
    "clients:create",
    "clients:update",
    "clients:delete",
    "service_blueprints:view",
    "service_blueprints:create",
    "service_blueprints:update",
    "service_blueprints:delete",
    "service_blueprints:deploy",
    "deployment_requests:request",
    "deployment_requests:view",
    "change_bundles:create",
    "change_bundles:view",
    "components:view",
    "components:create",
    "components:update",
    "components:delete",
    "components:check",
]

HERMES_AGENT_PERMISSIONS = [
    "clusters:view",
    "overview:view",
    "namespaces:view",
    "pods:view",
    "deployments:view",
    "services:view",
    "alerts:view",
    "app_services:view",
    "clients:view",
    "service_blueprints:view",
    "components:view",
]

ROLE_DEFINITIONS = {
    "admin": {
        "description": "Full access to everything",
        "is_system_role": True,
        "permissions": ALL_PERMISSION_KEYS,
    },
    "operator": {
        "description": "Operate clusters, alerts, and upgrade prechecks",
        "is_system_role": True,
        "permissions": OPERATOR_PERMISSIONS,
    },
    "cluster_admin": {
        "description": "Manage inventory catalog and deploy applications",
        "is_system_role": True,
        "permissions": CLUSTER_ADMIN_PERMISSIONS,
    },
    "viewer": {
        "description": "Read-only access to allowed clusters and namespaces",
        "is_system_role": True,
        "permissions": VIEWER_PERMISSIONS,
    },
    "hermes-agent": {
        "description": "Read-only service account for the Hermes AI Operations Agent",
        "is_system_role": True,
        "permissions": HERMES_AGENT_PERMISSIONS,
    },
}

DEFAULT_USERS = [
    {
        "username": "admin",
        "password": "admin123",
        "email": "admin@kubesight.local",
        "full_name": "Cluster Admin",
        "role": "admin",
    },
    {
        "username": "viewer",
        "password": "viewer123",
        "email": "viewer@kubesight.local",
        "full_name": "Read Only User",
        "role": "viewer",
    },
    {
        "username": "operator",
        "password": "operator123",
        "email": "operator@kubesight.local",
        "full_name": "Platform Operator",
        "role": "operator",
    },
    {
        "username": "hermes-agent",
        "password": "hermes-agent-disabled",
        "email": "hermes@kubesight.local",
        "full_name": "Hermes AI Agent",
        "role": "hermes-agent",
    },
]
