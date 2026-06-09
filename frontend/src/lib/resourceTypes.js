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
];

export const RESOURCE_TAB_DEFINITIONS = [
  { tabKey: "pods", listKey: "pods", title: "Pods", permission: "pods:view", resourceKind: "pod" },
  {
    tabKey: "deployments",
    listKey: "deployments",
    title: "Deployments",
    permission: "deployments:view",
    resourceKind: "deployment",
  },
  {
    tabKey: "replicaSets",
    listKey: "replicasets",
    title: "ReplicaSets",
    permission: "replicasets:view",
    resourceKind: "replicaset",
  },
  {
    tabKey: "statefulSets",
    listKey: "statefulsets",
    title: "StatefulSets",
    permission: "statefulsets:view",
    resourceKind: "statefulset",
  },
  {
    tabKey: "daemonSets",
    listKey: "daemonsets",
    title: "DaemonSets",
    permission: "daemonsets:view",
    resourceKind: "daemonset",
  },
  { tabKey: "jobs", listKey: "jobs", title: "Jobs", permission: "jobs:view", resourceKind: "job" },
  {
    tabKey: "cronJobs",
    listKey: "cronjobs",
    title: "CronJobs",
    permission: "cronjobs:view",
    resourceKind: "cronjob",
  },
  {
    tabKey: "services",
    listKey: "services",
    title: "Services",
    permission: "services:view",
    resourceKind: "service",
  },
];

export function emptyNamespaceResources() {
  return Object.fromEntries(NAMESPACE_RESOURCE_LIST_KEYS.map((key) => [key, []]));
}

export function emptyNamespaceResourceBucket() {
  return Object.fromEntries(NAMESPACE_RESOURCE_LIST_KEYS.map((key) => [key, []]));
}

export function listKeyForTab(tabKey) {
  return RESOURCE_TAB_DEFINITIONS.find((def) => def.tabKey === tabKey)?.listKey || tabKey;
}
