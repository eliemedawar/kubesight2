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
    ("registries:view", "View linked image registries"),
    ("registries:manage", "Add, edit, and remove linked image registries"),
    ("ticketing:view", "View the ticketing integrations (Jira, Zoho) and their status"),
    ("ticketing:manage", "Configure a ticketing integration and trigger a sync"),
    ("mobile_apps:view", "View mobile applications and download builds"),
    ("mobile_apps:manage", "Register mobile applications and configure store credentials"),
    ("cluster_builds:view", "View cluster builds"),
    ("cluster_builds:create", "Create and edit cluster builds"),
    ("cluster_builds:execute", "Run cluster build preflights and executions"),
    (
        "cluster_builds:kubeconfig",
        "Download the cluster-admin kubeconfig of a cluster KubeSight built",
    ),
    ("ssh_credentials:manage", "Manage SSH credentials and connection profiles"),
    ("vsphere:manage", "Manage vSphere connections and browse VM inventory"),
    ("applications:view", "View Application Intelligence applications and analyses"),
    ("applications:manage", "Create and update Application Intelligence applications"),
    ("applications:analyze", "Request source-code analysis through Hermes"),
    (
        "applications:execute",
        "Execute the bounded Application Intelligence analysis pipeline as a service account",
    ),
    ("ci_services:view", "View the CI Service Catalog"),
    ("ci_services:create", "Register services in the CI Service Catalog"),
    ("ci_services:edit", "Edit a CI service and its source configuration"),
    ("ci_services:delete", "Delete a CI service and its build history"),
    ("ci_pipelines:view", "View CI pipeline definitions"),
    ("ci_pipelines:edit", "Create and edit CI pipeline definitions"),
    ("ci_builds:view", "View CI builds, stages, and build logs"),
    ("ci_builds:run", "Trigger CI builds"),
    ("ci_builds:cancel", "Cancel running or queued CI builds"),
    ("ci_builds:retry", "Retry CI builds"),
    ("ci_artifacts:view", "View and download CI build artifacts"),
    ("ci_runners:view", "View CI runners and their capacity"),
    ("ci_runners:manage", "Enable, label, and configure CI runners"),
    ("ci_secrets:view", "View the names of CI secrets (never their values)"),
    ("ci_secrets:manage", "Create, update, and delete CI secrets and source credentials"),
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
    {
        "id": "clusterBuilds",
        "label": "Cluster Builder",
        "keys": [
            "cluster_builds:view", "cluster_builds:create", "cluster_builds:execute",
            "cluster_builds:kubeconfig",
            "ssh_credentials:manage", "vsphere:manage",
        ],
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
        "id": "applicationIntelligence",
        "label": "Application Intelligence",
        "keys": [
            "applications:view",
            "applications:manage",
            "applications:analyze",
            "applications:execute",
        ],
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
        "id": "mobileApps",
        "label": "Mobile Applications",
        "keys": ["mobile_apps:view", "mobile_apps:manage"],
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
        "id": "ci",
        "label": "CI / Service Catalog",
        "keys": [
            "ci_services:view", "ci_services:create", "ci_services:edit", "ci_services:delete",
            "ci_pipelines:view", "ci_pipelines:edit",
            "ci_builds:view", "ci_builds:run", "ci_builds:cancel", "ci_builds:retry",
            "ci_artifacts:view",
            "ci_runners:view", "ci_runners:manage",
            "ci_secrets:view", "ci_secrets:manage",
        ],
    },
    {
        "id": "administration",
        "label": "Administration",
        "keys": [
            "users:view", "users:manage", "users:create", "users:update", "users:disable", "users:delete",
            "roles:view", "roles:manage", "settings:view", "settings:manage",
            "audit:view", "api_tokens:manage", "registries:view", "registries:manage",
            "ticketing:view", "ticketing:manage",
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
    "components:delete", "registries:manage", "mobile_apps:manage",
    "cluster_builds:create", "cluster_builds:execute",
    "applications:manage", "applications:analyze", "applications:execute",
    # Handing out a cluster-admin kubeconfig is the most privileged thing this
    # feature can do — it is full control of the cluster, outside KubeSight's
    # own RBAC, for as long as the certificate lives.
    "cluster_builds:kubeconfig",
    "ssh_credentials:manage", "vsphere:manage",
    # Running a build executes repository-controlled commands on a runner, and
    # editing a pipeline decides which commands those are.
    "ci_services:delete", "ci_pipelines:edit", "ci_builds:run", "ci_secrets:manage",
    "ci_runners:manage",
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
    "applications:view",
    "ci_services:view",
    "ci_pipelines:view",
    "ci_builds:view",
    "ci_artifacts:view",
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
    "mobile_apps:view",
    "applications:view",
    "applications:manage",
    "applications:analyze",
    # Operators run and watch builds but do not reshape the catalog.
    "ci_services:view",
    "ci_pipelines:view",
    "ci_builds:view",
    "ci_builds:run",
    "ci_builds:cancel",
    "ci_builds:retry",
    "ci_artifacts:view",
    "ci_runners:view",
    "ci_secrets:view",
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
    "mobile_apps:view",
    "applications:view",
    "applications:manage",
    "applications:analyze",
    "ci_services:view",
    "ci_services:create",
    "ci_services:edit",
    "ci_pipelines:view",
    "ci_pipelines:edit",
    "ci_builds:view",
    "ci_builds:run",
    "ci_builds:cancel",
    "ci_builds:retry",
    "ci_artifacts:view",
    "ci_runners:view",
    "ci_secrets:view",
    "ci_secrets:manage",
]

HERMES_AGENT_PERMISSIONS = [
    "applications:execute",
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
        "description": "Non-interactive, source-analysis-only Hermes service account",
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
