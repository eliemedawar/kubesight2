/**
 * Admin-facing "Allowed Actions" mapped to backend permission keys.
 * UI never shows raw permission keys to admins.
 */

export const ALLOWED_ACTIONS = [
  {
    id: "view_resources",
    label: "View Resources",
    description: "See workload and service objects in selected namespaces.",
    permissions: [
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
    resourceTypes: [
      "pod",
      "deployment",
      "replicaset",
      "statefulset",
      "daemonset",
      "job",
      "cronjob",
      "service",
    ],
  },
  {
    id: "view_logs",
    label: "View Logs",
    description: "Stream logs for selected pods.",
    permissions: ["logs:view"],
    resourceTypes: ["pod"],
  },
  {
    id: "view_metrics",
    label: "View Metrics",
    description: "See usage and health metrics for assigned workloads.",
    permissions: ["overview:view", "resources:view"],
    resourceTypes: ["pod", "deployment", "service"],
    namespaceLevel: true,
  },
  {
    id: "view_alerts",
    label: "View Alerts",
    description: "See alerts for assigned clusters and namespaces.",
    permissions: ["alerts:view"],
    namespaceLevel: true,
    clusterLevel: true,
  },
  {
    id: "upgrade_precheck",
    label: "Run Upgrade Precheck",
    description: "Run upgrade safety checks on assigned clusters.",
    permissions: ["upgrades:precheck"],
    clusterLevel: true,
  },
  {
    id: "view_service_ports",
    label: "View Service Ports",
    description: "See exposed ports on selected services.",
    permissions: ["services:ports:view", "services:view"],
    resourceTypes: ["service"],
  },
];

export const BROWSER_RESOURCE_TYPES = [
  { value: "pod", label: "Pods", listKey: "pods", permissionKey: "pods:view" },
  {
    value: "deployment",
    label: "Deployments",
    listKey: "deployments",
    permissionKey: "deployments:view",
  },
  {
    value: "replicaset",
    label: "ReplicaSets",
    listKey: "replicasets",
    permissionKey: "replicasets:view",
  },
  {
    value: "statefulset",
    label: "StatefulSets",
    listKey: "statefulsets",
    permissionKey: "statefulsets:view",
  },
  {
    value: "daemonset",
    label: "DaemonSets",
    listKey: "daemonsets",
    permissionKey: "daemonsets:view",
  },
  { value: "job", label: "Jobs", listKey: "jobs", permissionKey: "jobs:view" },
  {
    value: "cronjob",
    label: "CronJobs",
    listKey: "cronjobs",
    permissionKey: "cronjobs:view",
  },
  { value: "service", label: "Services", listKey: "services", permissionKey: "services:view" },
];

export const DEFAULT_ALLOWED_ACTIONS = ["view_resources", "view_logs", "view_metrics"];

export const FULL_CLUSTER_ACTION_IDS = [
  "view_resources",
  "view_logs",
  "view_metrics",
  "view_alerts",
  "view_service_ports",
  "upgrade_precheck",
];

export const NAMESPACE_DEFAULT_ACTION_IDS = [
  "view_resources",
  "view_logs",
  "view_metrics",
  "view_alerts",
  "view_service_ports",
];

/** True when every permission required by the action is granted on the role. */
export function actionAllowedForRole(action, rolePermissions = []) {
  const granted = new Set(rolePermissions || []);
  if (!granted.size) {
    return false;
  }
  return action.permissions.every((perm) => granted.has(perm));
}

export function selectableActionsForRole(role) {
  const perms = role?.permissions || [];
  if (role?.hasFullAccess) {
    return ALLOWED_ACTIONS;
  }
  return ALLOWED_ACTIONS.filter((action) => actionAllowedForRole(action, perms));
}

export function filterActionIdsForRole(actionIds, rolePermissions = []) {
  const selectable = new Set(selectableActionsForRole({ permissions: rolePermissions }).map((a) => a.id));
  return actionIds.filter((id) => selectable.has(id));
}

export function defaultActionIdsForRole(role) {
  const selectable = selectableActionsForRole(role);
  const preferred = DEFAULT_ALLOWED_ACTIONS.filter((id) => selectable.some((a) => a.id === id));
  return preferred.length ? preferred : selectable.map((a) => a.id);
}

export function actionPermissions(actionIds) {
  const perms = new Set();
  ALLOWED_ACTIONS.forEach((action) => {
    if (actionIds.includes(action.id)) {
      action.permissions.forEach((p) => perms.add(p));
    }
  });
  return [...perms];
}
