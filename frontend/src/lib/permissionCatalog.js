/** Human-readable permission catalog for Effective Permissions UI. */

export const PERMISSION_GROUPS = [
  {
    id: "dashboard",
    label: "Dashboard",
    keys: ["overview:view"],
  },
  {
    id: "clusters",
    label: "Clusters",
    keys: [
      "clusters:view",
      "clusters:add",
      "clusters:update",
      "clusters:remove",
      "clusters:test",
    ],
  },
  {
    id: "namespaces",
    label: "Namespaces",
    keys: ["namespaces:view"],
  },
  {
    id: "resources",
    label: "Resources",
    keys: [
      "resources:view",
      "pods:view",
      "deployments:view",
      "replicasets:view",
      "statefulsets:view",
      "daemonsets:view",
      "jobs:view",
      "cronjobs:view",
      "services:view",
      "services:ports:view",
    ],
  },
  {
    id: "logs",
    label: "Logs",
    keys: ["logs:view"],
  },
  {
    id: "alerts",
    label: "Alerts",
    keys: ["alerts:view", "alerts:manage"],
  },
  {
    id: "inventory",
    label: "Inventory",
    keys: [
      "inventory:view",
      "inventory:register",
      "inventory:update",
      "inventory:remove",
      "apps:deploy",
      "apps:dryrun",
      "apps:diff",
      "apps:delete",
      "helm:view",
      "helm:install",
      "helm:upgrade",
      "helm:rollback",
      "helm:uninstall",
      "helm:values:view",
      "helm:values:update",
    ],
  },
  {
    id: "upgrades",
    label: "Upgrade Safe Mode",
    keys: ["upgrades:precheck", "upgrades:start"],
  },
  {
    id: "userManagement",
    label: "User Management",
    keys: [
      "users:view",
      "users:manage",
      "users:create",
      "users:update",
      "users:disable",
      "roles:view",
      "roles:manage",
      "settings:view",
      "settings:manage",
    ],
  },
  {
    id: "audit",
    label: "Audit Logs",
    keys: ["audit:view"],
  },
];

export const PERMISSION_CATALOG = [
  { key: "users:view", label: "View users", dangerous: false },
  { key: "users:manage", label: "Manage users and roles", dangerous: true },
  { key: "users:create", label: "Create users", dangerous: true },
  { key: "users:update", label: "Update users", dangerous: true },
  { key: "users:disable", label: "Disable users", dangerous: true },
  { key: "roles:view", label: "View roles", dangerous: false },
  { key: "roles:manage", label: "Manage role permissions", dangerous: true },
  { key: "clusters:view", label: "View clusters", dangerous: false },
  { key: "clusters:add", label: "Add clusters", dangerous: true },
  { key: "clusters:update", label: "Update clusters", dangerous: true },
  { key: "clusters:remove", label: "Remove clusters", dangerous: true },
  { key: "clusters:test", label: "Test cluster connections", dangerous: false },
  { key: "overview:view", label: "View cluster overview", dangerous: false },
  { key: "namespaces:view", label: "View namespaces", dangerous: false },
  { key: "resources:view", label: "View resources", dangerous: false },
  { key: "pods:view", label: "View pods", dangerous: false },
  { key: "deployments:view", label: "View deployments", dangerous: false },
  { key: "replicasets:view", label: "View ReplicaSets", dangerous: false },
  { key: "statefulsets:view", label: "View StatefulSets", dangerous: false },
  { key: "daemonsets:view", label: "View DaemonSets", dangerous: false },
  { key: "jobs:view", label: "View Jobs", dangerous: false },
  { key: "cronjobs:view", label: "View CronJobs", dangerous: false },
  { key: "logs:view", label: "View logs", dangerous: false },
  { key: "alerts:view", label: "View alerts", dangerous: false },
  { key: "alerts:manage", label: "Manage alerts", dangerous: true },
  { key: "upgrades:precheck", label: "Run upgrade prechecks", dangerous: false },
  { key: "upgrades:start", label: "Start upgrades", dangerous: true },
  { key: "settings:view", label: "View settings", dangerous: false },
  { key: "settings:manage", label: "Manage settings", dangerous: true },
  { key: "audit:view", label: "View audit logs", dangerous: false },
  { key: "services:view", label: "View services", dangerous: false },
  { key: "services:ports:view", label: "View service ports", dangerous: false },
  { key: "inventory:view", label: "View application inventory", dangerous: false },
  { key: "inventory:register", label: "Register applications in inventory", dangerous: false },
  { key: "inventory:update", label: "Update application catalog metadata", dangerous: false },
  { key: "inventory:remove", label: "Remove applications from inventory", dangerous: true },
  { key: "apps:deploy", label: "Deploy applications to clusters", dangerous: true },
  { key: "apps:dryrun", label: "Run deployment dry-run", dangerous: false },
  { key: "apps:diff", label: "View deployment diffs", dangerous: false },
  { key: "apps:delete", label: "Delete applications from clusters", dangerous: true },
  { key: "helm:view", label: "View Helm releases", dangerous: false },
  { key: "helm:install", label: "Install Helm releases", dangerous: true },
  { key: "helm:upgrade", label: "Upgrade Helm releases", dangerous: true },
  { key: "helm:rollback", label: "Rollback Helm releases", dangerous: true },
  { key: "helm:uninstall", label: "Uninstall Helm releases", dangerous: true },
  { key: "helm:values:view", label: "View Helm release values", dangerous: false },
  { key: "helm:values:update", label: "Update Helm release values", dangerous: true },
];

const BLOCKED_HIGHLIGHT_KEYS = new Set([
  "users:create",
  "users:update",
  "users:disable",
  "roles:manage",
  "clusters:add",
  "clusters:remove",
  "settings:manage",
  "upgrades:start",
  "apps:deploy",
  "apps:delete",
  "helm:install",
  "helm:upgrade",
  "helm:uninstall",
]);

export function getEffectivePermissions(role) {
  const granted = new Set(role?.permissions || []);
  const allowed = [];
  const blocked = [];

  PERMISSION_CATALOG.forEach((entry) => {
    if (granted.has(entry.key)) {
      allowed.push(entry);
    } else if (BLOCKED_HIGHLIGHT_KEYS.has(entry.key) || entry.dangerous) {
      blocked.push(entry);
    }
  });

  return { allowed, blocked };
}

export function permissionLabel(key) {
  return PERMISSION_CATALOG.find((p) => p.key === key)?.label || key;
}

export function permissionSummary(permissions = [], maxItems = 4) {
  const keys = [...permissions].sort();
  if (!keys.length) {
    return "No permissions";
  }
  const labels = keys.slice(0, maxItems).map(permissionLabel);
  if (keys.length > maxItems) {
    labels.push(`+${keys.length - maxItems} more`);
  }
  return labels.join(", ");
}

export function catalogEntriesForGroup(group) {
  const keySet = new Set(group.keys);
  return PERMISSION_CATALOG.filter((entry) => keySet.has(entry.key));
}
