/** Human-friendly role descriptions for the user form. */

export const ROLE_PRESETS = {
  admin: {
    label: "Admin",
    description: "Full platform access. Can manage users, settings, and all clusters.",
    sidebarPages: "All navigation items",
    assignableActions: "All allowed actions in the access editor",
  },
  operator: {
    label: "Operator",
    description: "Can view resources, logs, alerts, and run upgrade prechecks on assigned clusters.",
    sidebarPages: "Dashboard, Clusters, Overview, Namespaces, Resources, Logs, Alerts, Upgrade",
    assignableActions: "View resources/logs/metrics/alerts, service ports, upgrade precheck",
  },
  viewer: {
    label: "Viewer",
    description: "Can view assigned resources and logs. Cannot change settings or manage users.",
    sidebarPages: "Dashboard, Clusters, Overview, Namespaces, Resources, Logs, Alerts",
    assignableActions: "View resources, logs, metrics, alerts, and service ports only",
  },
  cluster_admin: {
    label: "Cluster Admin",
    description: "Manage inventory catalog and deploy applications on assigned clusters.",
    sidebarPages: "Dashboard, Clusters, Overview, Namespaces, Resources, Inventory, Logs, Alerts, Upgrade",
    assignableActions: "Inventory, deploy, Helm, and operational actions on assigned clusters",
  },
};

export function roleDescription(roleName, roleObject = null) {
  if (roleObject?.description) {
    return roleObject.description;
  }
  if (!roleName) return "";
  const key = String(roleName).toLowerCase();
  if (ROLE_PRESETS[key]) {
    return ROLE_PRESETS[key].description;
  }
  return `Role: ${roleName}`;
}

export function roleDisplayLabel(roleName) {
  const key = String(roleName || "").toLowerCase();
  if (ROLE_PRESETS[key]?.label) {
    return ROLE_PRESETS[key].label;
  }
  return roleName;
}
