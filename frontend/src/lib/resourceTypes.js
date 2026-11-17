/** Namespace resource tabs and API list keys for the Resources page. */

export const WORKLOAD_VIEW_PERMISSIONS = [
  "replicasets:view",
  "statefulsets:view",
  "daemonsets:view",
  "jobs:view",
  "cronjobs:view",
];

export const NAMESPACE_RESOURCE_LIST_KEYS = [
  "pods",
  "deployments",
  "replicasets",
  "statefulsets",
  "daemonsets",
  "jobs",
  "cronjobs",
  "services",
  "configmaps",
  "secrets",
  "ingress",
  "events",
  "hpas",
  "networkpolicies",
  "pvcs",
  "pvs",
  "storageclasses",
];

/**
 * One entry per kind the page lists. The order is the fallback order when a
 * URL names a tab this user cannot see (Pods first); the side rail is ordered
 * by RESOURCE_GROUPS instead.
 *
 * - `apiKind`: the Kubernetes kind, shown next to the friendly title
 * - `clusterScoped`: not namespaced; the namespace picker does not narrow it
 * - `describe(ns)`: the one sentence under the title
 */
export const RESOURCE_TAB_DEFINITIONS = [
  {
    tabKey: "pods",
    listKey: "pods",
    title: "Pods",
    apiKind: "Pod",
    permission: "pods:view",
    resourceKind: "pod",
    describe: (ns) => `Running containers in ${ns}, with live status, restarts and usage against limits.`,
  },
  {
    tabKey: "deployments",
    listKey: "deployments",
    title: "Deployments",
    apiKind: "Deployment",
    permission: "deployments:view",
    resourceKind: "deployment",
    describe: () => "Stateless apps and their rollout state. Restart, roll back and edit from the row.",
  },
  {
    tabKey: "configMaps",
    listKey: "configmaps",
    title: "ConfigMaps",
    apiKind: "ConfigMap",
    permission: "resources:view",
    resourceKind: "configmap",
    describe: () => "Plain settings mounted into pods.",
  },
  {
    tabKey: "secrets",
    listKey: "secrets",
    title: "Secrets",
    apiKind: "Secret",
    permission: "resources:view",
    resourceKind: "secret",
    describe: () => "Credentials and certificates. Values stay hidden unless your role may reveal them.",
  },
  {
    tabKey: "replicaSets",
    listKey: "replicasets",
    title: "ReplicaSets",
    apiKind: "ReplicaSet",
    permission: "replicasets:view",
    resourceKind: "replicaset",
    describe: () => "Kept by deployments for each version. An old one at 0 is what a rollback returns to.",
  },
  {
    tabKey: "statefulSets",
    listKey: "statefulsets",
    title: "StatefulSets",
    apiKind: "StatefulSet",
    permission: "statefulsets:view",
    resourceKind: "statefulset",
    describe: () => "Apps that keep their identity and their own volume, such as databases.",
  },
  {
    tabKey: "daemonSets",
    listKey: "daemonsets",
    title: "DaemonSets",
    apiKind: "DaemonSet",
    permission: "daemonsets:view",
    resourceKind: "daemonset",
    describe: () => "One pod on every matching node.",
  },
  {
    tabKey: "jobs",
    listKey: "jobs",
    title: "Jobs",
    apiKind: "Job",
    permission: "jobs:view",
    resourceKind: "job",
    describe: () => "Run-to-completion tasks.",
  },
  {
    tabKey: "cronJobs",
    listKey: "cronjobs",
    title: "CronJobs",
    apiKind: "CronJob",
    permission: "cronjobs:view",
    resourceKind: "cronjob",
    describe: () => "Jobs on a schedule.",
  },
  {
    tabKey: "services",
    listKey: "services",
    title: "Services",
    apiKind: "Service",
    permission: "services:view",
    resourceKind: "service",
    describe: () => "Stable addresses in front of pods.",
  },
  {
    tabKey: "ingresses",
    listKey: "ingress",
    title: "Ingresses",
    apiKind: "Ingress",
    permission: "resources:view",
    resourceKind: "ingress",
    describe: () => "Hostnames routed into services from outside the cluster.",
  },
  {
    tabKey: "events",
    listKey: "events",
    title: "Events",
    apiKind: "Event",
    permission: "resources:view",
    resourceKind: "event",
    describe: (ns) =>
      `What Kubernetes reported in ${ns} recently. Click an object to open it.`,
  },
  {
    tabKey: "hpas",
    listKey: "hpas",
    title: "Autoscalers",
    apiKind: "HorizontalPodAutoscaler",
    permission: "resources:view",
    resourceKind: "hpa",
    describe: () =>
      "Grow and shrink workloads on CPU or memory. “At max replicas” means it wants more pods than it is allowed.",
  },
  {
    tabKey: "networkPolicies",
    listKey: "networkpolicies",
    title: "Network policies",
    apiKind: "NetworkPolicy",
    permission: "resources:view",
    resourceKind: "networkpolicy",
    describe: () =>
      "Which traffic may reach pods and leave them. A pod no policy selects accepts everything.",
  },
  {
    tabKey: "pvcs",
    listKey: "pvcs",
    title: "Volume claims",
    apiKind: "PersistentVolumeClaim",
    permission: "resources:view",
    resourceKind: "pvc",
    describe: (ns) => `Storage requested by pods in ${ns}, and whether each claim got a volume.`,
  },
  {
    tabKey: "pvs",
    listKey: "pvs",
    title: "Volumes",
    apiKind: "PersistentVolume",
    permission: "resources:view",
    resourceKind: "pv",
    clusterScoped: true,
    describe: (ns) =>
      `The disks themselves. They belong to the whole cluster, so the list opens on those claimed from ${ns}.`,
  },
  {
    tabKey: "storageClasses",
    listKey: "storageclasses",
    title: "Storage classes",
    apiKind: "StorageClass",
    permission: "resources:view",
    resourceKind: "storageclass",
    clusterScoped: true,
    describe: () =>
      "The kinds of storage this cluster can create. A claim that names no class gets the default.",
  },
];

/** The side rail: kinds grouped by what they do. */
export const RESOURCE_GROUPS = [
  { key: "activity", label: "Activity", tabs: ["events"] },
  {
    key: "workloads",
    label: "Workloads",
    tabs: ["pods", "deployments", "statefulSets", "daemonSets", "replicaSets", "jobs", "cronJobs", "hpas"],
  },
  { key: "network", label: "Network", tabs: ["services", "ingresses", "networkPolicies"] },
  { key: "config", label: "Config", tabs: ["configMaps", "secrets"] },
  { key: "storage", label: "Storage", tabs: ["pvcs", "pvs", "storageClasses"] },
];

const TAB_BY_KEY = new Map(RESOURCE_TAB_DEFINITIONS.map((def) => [def.tabKey, def]));
const TAB_BY_LIST_KEY = new Map(RESOURCE_TAB_DEFINITIONS.map((def) => [def.listKey, def]));

export function tabDefinition(tabKey) {
  return TAB_BY_KEY.get(tabKey) || null;
}

export function tabKeyForListKey(listKey) {
  return TAB_BY_LIST_KEY.get(listKey)?.tabKey || "";
}

/** The tab that lists an object of this Kubernetes kind ("Pod" → "pods"). */
export function tabKeyForApiKind(apiKind) {
  return RESOURCE_TAB_DEFINITIONS.find((def) => def.apiKind === apiKind)?.tabKey || "";
}

export function emptyNamespaceResources() {
  return Object.fromEntries(NAMESPACE_RESOURCE_LIST_KEYS.map((key) => [key, []]));
}

export function emptyNamespaceResourceBucket() {
  return Object.fromEntries(NAMESPACE_RESOURCE_LIST_KEYS.map((key) => [key, []]));
}

export function listKeyForTab(tabKey) {
  return RESOURCE_TAB_DEFINITIONS.find((def) => def.tabKey === tabKey)?.listKey || tabKey;
}

// --- What needs attention ----------------------------------------------------
// Mirrors backend/api/services/resource_summary.py: the rail badge (counted on
// the server) and the "Needs attention" filter (applied here) must agree.

const HEALTHY_STATUSES = new Set([
  "running",
  "completed",
  "complete",
  "succeeded",
  "active",
  "available",
  "healthy",
  "ready",
  "bound",
  "updated",
  "true",
]);

const CRITICAL_STATUS = /err|crash|backoff|oom|evicted|fail|exitcode|signal|lost/;

const STATUS_FIELDS = {
  pods: ["status"],
  jobs: ["status"],
  deployments: ["updateStatus", "status"],
  statefulsets: ["updateStatus", "status"],
  daemonsets: ["updateStatus", "status"],
};

const HEALTH_KINDS = new Set(["hpas", "pvcs", "pvs"]);

/** "" for a fine status, otherwise "crit" or "warn". */
export function statusTone(status) {
  if (status == null) {
    return "";
  }
  const text = String(status).trim().toLowerCase();
  if (!text || text === "-" || text === "—" || HEALTHY_STATUSES.has(text)) {
    return "";
  }
  return CRITICAL_STATUS.test(text) ? "crit" : "warn";
}

/** How much a row of `listKey` needs attention: "", "warn" or "crit". */
export function rowTone(listKey, row, namespace) {
  if (!row) {
    return "";
  }
  const fields = STATUS_FIELDS[listKey];
  if (fields) {
    for (const field of fields) {
      const value = row[field];
      if (value != null && value !== "" && value !== "-") {
        return statusTone(value);
      }
    }
    return "";
  }
  if (HEALTH_KINDS.has(listKey)) {
    // Volumes are cluster-wide: only those claimed from here are this namespace's problem.
    if (listKey === "pvs" && row.claimNamespace !== namespace) {
      return "";
    }
    return row.health === "warn" || row.health === "crit" ? row.health : "";
  }
  if (listKey === "events") {
    return String(row.type || "").toLowerCase() === "warning" ? "warn" : "";
  }
  return "";
}

/** Whether a kind's rows can need attention (it gets the "Needs attention" filter). */
export function listKeyHasTone(listKey) {
  return Boolean(STATUS_FIELDS[listKey]) || HEALTH_KINDS.has(listKey);
}
