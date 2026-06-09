/**
 * Client-side authorization helpers aligned with backend access_engine.py.
 * UX filtering only — backend remains authoritative.
 */

import { getGrantedActionIds, hasGrantedAction, pageGrantedByAdmin } from "../lib/grantedAccess.js";
import {
  emptyNamespaceResources,
  NAMESPACE_RESOURCE_LIST_KEYS,
  RESOURCE_TAB_DEFINITIONS,
} from "../lib/resourceTypes.js";

const PERMISSION_ALIASES = {
  "pods:view": ["pods:view", "resources:view"],
  "deployments:view": ["deployments:view", "resources:view"],
  "replicasets:view": ["replicasets:view", "deployments:view", "resources:view"],
  "statefulsets:view": ["statefulsets:view", "deployments:view", "resources:view"],
  "daemonsets:view": ["daemonsets:view", "resources:view"],
  "jobs:view": ["jobs:view", "resources:view"],
  "cronjobs:view": ["cronjobs:view", "resources:view"],
  "services:view": ["services:view", "resources:view"],
  "resources:view": [
    "resources:view",
    "pods:view",
    "deployments:view",
    "replicasets:view",
    "statefulsets:view",
    "daemonsets:view",
    "jobs:view",
    "cronjobs:view",
    "services:view",
  ],
  "inventory:view": ["inventory:view", "resources:view"],
};

const RESOURCE_SPECIFICITY = {
  cluster: 10,
  namespace: 20,
  deployment: 30,
  replicaset: 30,
  statefulset: 30,
  daemonset: 30,
  job: 30,
  cronjob: 30,
  pod: 30,
  service: 30,
  container: 40,
  service_port: 45,
};

export const NAV_PAGES = [
  { key: "dashboard", label: "Dashboard", permission: "overview:view", section: "Dashboard" },
  { key: "clusters", label: "Clusters", permission: "clusters:view", section: "Infrastructure" },
  {
    key: "clusterOverview",
    label: "Cluster Overview",
    permission: "overview:view",
    section: "Infrastructure",
  },
  {
    key: "clusterManagement",
    label: "Clusters",
    anyPermissions: ["clusters:add", "clusters:update", "clusters:remove", "clusters:test"],
    section: "Infrastructure",
  },
  { key: "namespaces", label: "Namespaces", permission: "namespaces:view", section: "Infrastructure" },
  { key: "inventory", label: "Applications", permission: "inventory:view", section: "Workloads" },
  { key: "resources", label: "Resources", permission: "resources:view", section: "Workloads" },
  { key: "logs", label: "Logs", permission: "logs:view", section: "Monitoring" },
  { key: "alerts", label: "Alerts", permission: "alerts:view", section: "Monitoring" },
  {
    key: "alertPolicies",
    label: "Alert Policies",
    permission: "alerts:view",
    section: "Monitoring",
  },
  {
    key: "userManagement",
    label: "User Management",
    permission: "users:view",
    section: "Administration",
  },
  { key: "auditLogs", label: "Audit Logs", permission: "audit:view", section: "Administration" },
  { key: "settings", label: "Settings", permission: "settings:view", section: "Administration" },
  {
    key: "upgrade",
    label: "Upgrade Center",
    anyPermissions: ["upgrades:precheck", "upgrades:start"],
    section: "Operations",
  },
];

export const EMPTY_MESSAGES = {
  noClusters: "No clusters are assigned to your account. Contact an administrator.",
  noNamespaces: "No namespaces are assigned for this cluster.",
  noResources: "No resources are assigned to your account.",
  noLogPods: "No pods are assigned for log viewing.",
  noFeatures: "No features are assigned to your account. Contact an administrator.",
  noAccess: "You do not have access to this resource.",
  noAction: "You do not have access to this action.",
  unexpectedAccess:
    "Access was denied unexpectedly. Refresh the page or contact an administrator if this continues.",
};

/** Pages that use the topbar cluster selector. */
export const CLUSTER_CONTEXT_PAGE_KEYS = new Set([
  "dashboard",
  "clusters",
  "clusterOverview",
  "inventory",
  "applicationDetails",
  "namespaces",
  "resources",
  "logs",
  "alerts",
  "alertPolicies",
  "upgrade",
]);

/** Pages that use the topbar namespace selector. */
export const NAMESPACE_CONTEXT_PAGE_KEYS = new Set([
  "namespaces",
  "resources",
  "logs",
]);

export function isAdminUser(user) {
  if (!user) {
    return false;
  }
  return user.isAdmin === true || user.hasFullAccess === true || user.role === "admin";
}

export function hasPermission(user, permissionKey) {
  if (!user) {
    return false;
  }
  if (isAdminUser(user)) {
    return true;
  }
  const keys = user.permissions || [];
  if (keys.includes(permissionKey)) {
    return true;
  }
  const aliases = PERMISSION_ALIASES[permissionKey] || [];
  return aliases.some((alias) => keys.includes(alias));
}

export function hasAnyPermission(user, permissionKeys) {
  return (permissionKeys || []).some((key) => hasPermission(user, key));
}

function ruleSpecificity(rule) {
  const base = RESOURCE_SPECIFICITY[rule.resourceType || "cluster"] || 5;
  let score = base;
  if (rule.resourceName) {
    score += 5;
  }
  if (rule.containerName) {
    score += 3;
  }
  if (rule.port != null && rule.port !== "") {
    score += 2;
  }
  if (rule.namespace) {
    score += 1;
  }
  return score;
}

function ruleMatches(
  rule,
  { clusterId, namespace, resourceType, resourceName, containerName, port, permissionKey }
) {
  if (rule.clusterId !== clusterId) {
    return false;
  }
  if (rule.permissionKey !== permissionKey) {
    return false;
  }

  const ruleNs = (rule.namespace || "").trim() || null;
  const reqNs = (namespace || "").trim() || null;
  const ruleRt = (rule.resourceType || "cluster").trim();
  const reqRt = (resourceType || "").trim() || null;

  if (ruleNs && reqNs && ruleNs !== reqNs) {
    return false;
  }
  if (ruleNs && !reqNs && ruleRt !== "cluster") {
    return false;
  }

  if (ruleRt === "cluster") {
    return true;
  }

  if (ruleRt === "namespace") {
    return reqNs === ruleNs ? Boolean(reqNs) : Boolean(reqNs);
  }

  if (!reqRt) {
    return false;
  }
  if (ruleRt !== reqRt) {
    return false;
  }

  const ruleName = (rule.resourceName || "").trim() || null;
  const reqName = (resourceName || "").trim() || null;
  if (ruleName && reqName && ruleName !== reqName) {
    return false;
  }
  if (ruleName && !reqName) {
    return false;
  }

  if (["pod", "container"].includes(ruleRt) && rule.containerName) {
    const reqContainer = (containerName || "").trim() || null;
    if (reqContainer && rule.containerName !== reqContainer) {
      return false;
    }
  }

  if (ruleRt === "service_port") {
    if (rule.port != null && port != null && Number(rule.port) !== Number(port)) {
      return false;
    }
    if (rule.port != null && port == null) {
      return false;
    }
  }

  return true;
}

function legacyClusterIds(user) {
  return new Set(user.clusterAccess || []);
}

function legacyNamespacePairs(user) {
  return new Set(
    (user.namespaceAccess || []).map((row) => `${row.clusterId}\0${row.namespace}`)
  );
}

function loadRules(user) {
  return user.accessRules || [];
}

function usesLegacyOnly(user) {
  return loadRules(user).length === 0;
}

function legacyEvaluate(user, { clusterId, permissionKey, namespace, resourceType, resourceName }) {
  const clusters = legacyClusterIds(user);
  if (!clusters.has(clusterId)) {
    return false;
  }
  const nsPairs = legacyNamespacePairs(user);
  const clusterNs = [...nsPairs]
    .filter((pair) => pair.startsWith(`${clusterId}\0`))
    .map((pair) => pair.split("\0")[1]);
  if (!clusterNs.length) {
    return true;
  }
  if (!namespace) {
    return permissionKey === "clusters:view";
  }
  if (!clusterNs.includes(namespace)) {
    return false;
  }
  if (resourceType && resourceName) {
    return true;
  }
  return true;
}

export function evaluateAccess(
  user,
  {
    clusterId,
    permissionKey,
    namespace = null,
    resourceType = null,
    resourceName = null,
    containerName = null,
    port = null,
  }
) {
  if (!user) {
    return false;
  }
  if (isAdminUser(user)) {
    return true;
  }
  if (!hasPermission(user, permissionKey)) {
    return false;
  }

  const rules = loadRules(user);
  if (usesLegacyOnly(user)) {
    return legacyEvaluate(user, {
      clusterId,
      permissionKey,
      namespace,
      resourceType,
      resourceName,
    });
  }

  const matching = rules.filter((rule) =>
    ruleMatches(rule, {
      clusterId,
      namespace,
      resourceType,
      resourceName,
      containerName,
      port,
      permissionKey,
    })
  );

  if (!matching.length) {
    return false;
  }

  matching.sort((a, b) => ruleSpecificity(b) - ruleSpecificity(a));
  const topScore = ruleSpecificity(matching[0]);
  const top = matching.filter((rule) => ruleSpecificity(rule) === topScore);
  if (top.some((rule) => rule.effect === "deny")) {
    return false;
  }
  return top.some((rule) => rule.effect === "allow");
}

export function canAccessCluster(user, clusterId) {
  return evaluateAccess(user, {
    clusterId,
    permissionKey: "clusters:view",
    resourceType: "cluster",
  });
}

export function canAccessNamespace(user, clusterId, namespace) {
  return evaluateAccess(user, {
    clusterId,
    namespace,
    permissionKey: "namespaces:view",
    resourceType: "namespace",
  });
}

export function canAccessResource(user, clusterId, namespace, resourceType, resourceName) {
  const permMap = {
    pod: "pods:view",
    deployment: "deployments:view",
    replicaset: "replicasets:view",
    statefulset: "statefulsets:view",
    daemonset: "daemonsets:view",
    job: "jobs:view",
    cronjob: "cronjobs:view",
    service: "services:view",
  };
  const permissionKey = permMap[resourceType] || "resources:view";
  return evaluateAccess(user, {
    clusterId,
    namespace,
    permissionKey,
    resourceType,
    resourceName,
  });
}

export function canViewLogs(user, clusterId, namespace, podName, containerName = null) {
  if (
    evaluateAccess(user, {
      clusterId,
      namespace,
      permissionKey: "logs:view",
      resourceType: "pod",
      resourceName: podName,
      containerName,
    })
  ) {
    return true;
  }
  if (
    containerName &&
    evaluateAccess(user, {
      clusterId,
      namespace,
      permissionKey: "logs:view",
      resourceType: "container",
      resourceName: podName,
      containerName,
    })
  ) {
    return true;
  }
  return evaluateAccess(user, {
    clusterId,
    namespace,
    permissionKey: "logs:view",
    resourceType: "namespace",
  });
}

export function canViewAlert(user, alert) {
  if (!user || !alert) {
    return false;
  }
  if (isAdminUser(user)) {
    return true;
  }
  if (!hasPermission(user, "alerts:view")) {
    return false;
  }

  const clusterId = alert.clusterId || alert.cluster;
  if (!clusterId || !canAccessCluster(user, clusterId)) {
    return false;
  }

  const namespace = (alert.namespace || "").trim() || null;
  const resourceName = (alert.pod || alert.resourceName || alert.resource || "").trim() || null;

  if (resourceName && namespace) {
    if (
      evaluateAccess(user, {
        clusterId,
        namespace,
        permissionKey: "alerts:view",
        resourceType: "pod",
        resourceName,
      })
    ) {
      return true;
    }
  }

  if (namespace) {
    return evaluateAccess(user, {
      clusterId,
      namespace,
      permissionKey: "alerts:view",
      resourceType: "namespace",
    });
  }

  return evaluateAccess(user, {
    clusterId,
    permissionKey: "alerts:view",
    resourceType: "cluster",
  });
}

export function filterAlertsForUser(user, alerts = []) {
  return (alerts || []).filter((alert) => canViewAlert(user, alert));
}

export function canViewServicePort(user, clusterId, namespace, serviceName, port) {
  if (!hasPermission(user, "services:ports:view")) {
    return false;
  }
  return evaluateAccess(user, {
    clusterId,
    namespace,
    permissionKey: "services:ports:view",
    resourceType: "service_port",
    resourceName: serviceName,
    port,
  });
}

export function getAllowedClusters(user, clusters = []) {
  if (!user) {
    return [];
  }
  if (isAdminUser(user)) {
    return clusters;
  }
  const profileIds = getProfileClusterIds(user);
  return clusters.filter((cluster) => {
    if (!cluster?.id) {
      return false;
    }
    if (profileIds && !profileIds.has(cluster.id)) {
      return false;
    }
    return canAccessCluster(user, cluster.id);
  });
}

export function getAllowedNamespaces(user, clusterId, namespaces = []) {
  if (!user || !clusterId) {
    return [];
  }
  if (isAdminUser(user)) {
    return namespaces;
  }
  return namespaces.filter((ns) => {
    const name = typeof ns === "string" ? ns : ns?.name;
    return name && canAccessNamespace(user, clusterId, name);
  });
}

function canAccessReplicaSet(user, clusterId, namespace, replicaSet) {
  const name = replicaSet?.name;
  if (!name) {
    return false;
  }
  const owner = String(replicaSet.owner || "").trim();
  if (owner && owner !== "-" && canAccessResource(user, clusterId, namespace, "deployment", owner)) {
    return true;
  }
  return canAccessResource(user, clusterId, namespace, "replicaset", name);
}

export function getAllowedResources(user, clusterId, namespace, resources = {}) {
  if (!user) {
    return emptyNamespaceResources();
  }
  if (isAdminUser(user)) {
    return Object.fromEntries(
      NAMESPACE_RESOURCE_LIST_KEYS.map((key) => [key, resources[key] || []])
    );
  }

  const pods = (resources.pods || []).filter(
    (pod) => pod?.name && canAccessResource(user, clusterId, namespace, "pod", pod.name)
  );

  const deployments = (resources.deployments || []).filter(
    (dep) => dep?.name && canAccessResource(user, clusterId, namespace, "deployment", dep.name)
  );

  const replicasets = (resources.replicasets || []).filter((rs) =>
    canAccessReplicaSet(user, clusterId, namespace, rs)
  );

  const statefulsets = (resources.statefulsets || []).filter(
    (item) =>
      item?.name && canAccessResource(user, clusterId, namespace, "statefulset", item.name)
  );

  const daemonsets = (resources.daemonsets || []).filter(
    (item) => item?.name && canAccessResource(user, clusterId, namespace, "daemonset", item.name)
  );

  const jobs = (resources.jobs || []).filter(
    (item) => item?.name && canAccessResource(user, clusterId, namespace, "job", item.name)
  );

  const cronjobs = (resources.cronjobs || []).filter(
    (item) => item?.name && canAccessResource(user, clusterId, namespace, "cronjob", item.name)
  );

  const services = (resources.services || [])
    .filter((svc) => svc?.name && canAccessResource(user, clusterId, namespace, "service", svc.name))
    .map((svc) => {
      if (!hasPermission(user, "services:ports:view")) {
        return { ...svc, ports: [], portsDetail: [], canViewPorts: false };
      }
      const portsRaw = svc.ports || [];
      const portList = Array.isArray(portsRaw)
        ? portsRaw
        : String(portsRaw)
            .split(",")
            .map((p) => p.trim())
            .filter(Boolean);
      const visiblePorts = portList.filter((p) => {
        const portNum = Number(p);
        return (
          Number.isFinite(portNum) &&
          canViewServicePort(user, clusterId, namespace, svc.name, portNum)
        );
      });
      return {
        ...svc,
        ports: visiblePorts,
        canViewPorts: visiblePorts.length > 0,
      };
    });

  return {
    pods,
    deployments,
    replicasets,
    statefulsets,
    daemonsets,
    jobs,
    cronjobs,
    services,
  };
}

export function getLogVisiblePods(user, clusterId, namespace, pods = []) {
  if (!hasPermission(user, "logs:view")) {
    return [];
  }
  return (pods || []).filter((pod) => {
    if (pod.canViewLogs === false) {
      return false;
    }
    if (pod.canViewLogs === true) {
      return true;
    }
    return pod?.name && canViewLogs(user, clusterId, namespace, pod.name);
  });
}

function pageNavPermissionAllowed(user, page) {
  if (page.anyPermissions?.length) {
    return hasAnyPermission(user, page.anyPermissions);
  }
  if (!page.permission) {
    return true;
  }
  return hasPermission(user, page.permission);
}

const CLUSTER_SCOPE_PERMISSIONS = new Set([
  "clusters:view",
  "overview:view",
  "namespaces:view",
  "resources:view",
  "inventory:view",
  "pods:view",
  "deployments:view",
  "replicasets:view",
  "statefulsets:view",
  "daemonsets:view",
  "jobs:view",
  "cronjobs:view",
  "services:view",
  "logs:view",
  "alerts:view",
]);

/** Cluster IDs granted by profile access rules or legacy clusterAccess (aligned with backend). */
export function getProfileClusterIds(user) {
  if (!user || isAdminUser(user)) {
    return null;
  }
  const rules = loadRules(user);
  if (rules.length) {
    return new Set(
      rules
        .filter(
          (rule) =>
            rule.effect === "allow" &&
            rule.clusterId &&
            CLUSTER_SCOPE_PERMISSIONS.has(rule.permissionKey)
        )
        .map((rule) => rule.clusterId)
    );
  }
  return new Set(user.clusterAccess || []);
}

export function hasAnyClusterAccess(user) {
  if (!user) {
    return false;
  }
  if (isAdminUser(user)) {
    return true;
  }
  const profileIds = getProfileClusterIds(user);
  return Boolean(profileIds?.size);
}

export function hasAnyNamespaceAccess(user) {
  if (!user) {
    return false;
  }
  if (isAdminUser(user)) {
    return true;
  }
  if (usesLegacyOnly(user)) {
    if (!legacyClusterIds(user).size) {
      return false;
    }
    if (!legacyNamespacePairs(user).size) {
      return true;
    }
    return legacyNamespacePairs(user).size > 0;
  }
  return loadRules(user).some(
    (rule) => rule.effect === "allow" && rule.permissionKey === "namespaces:view"
  );
}

function hasAllowRuleForPermissions(user, permissionKeys) {
  if (!user || isAdminUser(user)) {
    return true;
  }
  if (usesLegacyOnly(user)) {
    return legacyClusterIds(user).size > 0;
  }
  const keys = new Set(permissionKeys);
  return loadRules(user).some((rule) => rule.effect === "allow" && keys.has(rule.permissionKey));
}

export function canAccessResourcesPage(user) {
  const resourcePerms = [
    "resources:view",
    "inventory:view",
    "pods:view",
    "deployments:view",
    "replicasets:view",
    "statefulsets:view",
    "daemonsets:view",
    "jobs:view",
    "cronjobs:view",
    "services:view",
  ];
  if (!hasAnyPermission(user, resourcePerms)) {
    return false;
  }
  if (isAdminUser(user)) {
    return true;
  }
  return hasGrantedAction(user, "view_resources") && hasAnyClusterAccess(user);
}

export function canAccessLogsPage(user) {
  if (!hasPermission(user, "logs:view")) {
    return false;
  }
  if (isAdminUser(user)) {
    return true;
  }
  return hasGrantedAction(user, "view_logs");
}

export function canViewResourceTab(user, tabKey) {
  if (!isAdminUser(user) && !hasGrantedAction(user, "view_resources")) {
    return false;
  }
  const tabDef = RESOURCE_TAB_DEFINITIONS.find((def) => def.tabKey === tabKey);
  if (!tabDef) {
    return false;
  }
  return hasPermission(user, tabDef.permission) || hasPermission(user, "resources:view");
}

export function getVisibleResourceTabs(user) {
  return RESOURCE_TAB_DEFINITIONS.filter((def) => canViewResourceTab(user, def.tabKey)).map(
    (def) => def.tabKey
  );
}

export function pageNeedsClusterContext(pageKey) {
  return CLUSTER_CONTEXT_PAGE_KEYS.has(pageKey);
}

export function pageNeedsNamespaceContext(pageKey) {
  return NAMESPACE_CONTEXT_PAGE_KEYS.has(pageKey);
}

/** Routes reachable from nav but not listed in NAV_PAGES (sidebar). */
const DRILL_DOWN_PAGES = new Set(["applicationDetails"]);

export function pageAllowed(user, pageKey) {
  if (DRILL_DOWN_PAGES.has(pageKey)) {
    switch (pageKey) {
      case "applicationDetails":
        return isAdminUser(user) && canAccessResourcesPage(user);
      default:
        return false;
    }
  }

  const page = NAV_PAGES.find((p) => p.key === pageKey);
  if (!page) {
    return false;
  }
  if (!pageNavPermissionAllowed(user, page)) {
    return false;
  }

  switch (pageKey) {
    case "dashboard":
    case "clusterOverview":
      return (
        hasPermission(user, "overview:view") &&
        (isAdminUser(user) || hasAnyClusterAccess(user))
      );
    case "clusters":
      return hasPermission(user, "clusters:view") && (isAdminUser(user) || hasAnyClusterAccess(user));
    case "clusterManagement":
      return hasAnyPermission(user, [
        "clusters:add",
        "clusters:update",
        "clusters:remove",
        "clusters:test",
      ]);
    case "namespaces":
      return (
        hasPermission(user, "namespaces:view") &&
        (isAdminUser(user) ||
          (pageGrantedByAdmin(user, "namespaces") && hasAnyNamespaceAccess(user)))
      );
    case "inventory":
      return isAdminUser(user) && canAccessResourcesPage(user);
    case "resources":
      return canAccessResourcesPage(user);
    case "logs":
      return canAccessLogsPage(user);
    case "alerts":
    case "alertPolicies":
      return (
        hasPermission(user, "alerts:view") &&
        (isAdminUser(user) ||
          (pageGrantedByAdmin(user, "alerts") && hasAnyClusterAccess(user)))
      );
    case "upgrade":
      return (
        hasAnyPermission(user, ["upgrades:precheck", "upgrades:start"]) &&
        (isAdminUser(user) ||
          (pageGrantedByAdmin(user, "upgrade") && hasAnyClusterAccess(user)))
      );
    case "settings":
      return hasPermission(user, "settings:view");
    case "userManagement":
      return hasPermission(user, "users:view");
    case "auditLogs":
      return hasPermission(user, "audit:view");
    default:
      return pageNavPermissionAllowed(user, page);
  }
}

export function getVisiblePages(user) {
  let pages = NAV_PAGES.filter((page) => pageAllowed(user, page.key));
  if (
    !isAdminUser(user) &&
    pages.some((page) => page.key === "dashboard") &&
    pages.some((page) => page.key === "clusterOverview")
  ) {
    pages = pages.filter((page) => page.key !== "clusterOverview");
  }
  return pages;
}

export function getFirstAllowedPage(user) {
  if (pageAllowed(user, "dashboard")) {
    return "dashboard";
  }
  const visible = getVisiblePages(user);
  return visible[0]?.key || null;
}

export function isAccessDeniedError(message) {
  const text = String(message || "").trim().toLowerCase();
  if (!text) {
    return false;
  }
  return (
    text === "forbidden" ||
    text.includes("do not have access") ||
    text.includes("not authorized") ||
    text.includes("permission denied") ||
    text.includes("access was denied") ||
    text.includes("access denied")
  );
}

/** Map API errors for display; suppress routine access denials when requested. */
export function formatAccessError(message, { suppressAccessDenied = false } = {}) {
  const text = String(message || "").trim();
  if (!text) {
    return "";
  }
  if (isAccessDeniedError(text)) {
    return suppressAccessDenied ? "" : EMPTY_MESSAGES.noAccess;
  }
  return text;
}

/** True when the UI should surface a 403-style failure (unexpected backend denial). */
export function shouldShowAccessError(message, { expectedDenied = false } = {}) {
  if (!message) {
    return false;
  }
  if (expectedDenied) {
    return false;
  }
  return !formatAccessError(message, { suppressAccessDenied: true });
}

export function createAuthAccess(user) {
  return {
    user,
    isAdmin: isAdminUser(user),
    hasPermission: (permissionKey) => hasPermission(user, permissionKey),
    hasAnyPermission: (permissionKeys) => hasAnyPermission(user, permissionKeys),
    canAccessCluster: (clusterId) => canAccessCluster(user, clusterId),
    canAccessNamespace: (clusterId, namespace) => canAccessNamespace(user, clusterId, namespace),
    canAccessResource: (clusterId, namespace, resourceType, resourceName) =>
      canAccessResource(user, clusterId, namespace, resourceType, resourceName),
    canViewLogs: (clusterId, namespace, podName, containerName) =>
      canViewLogs(user, clusterId, namespace, podName, containerName),
    canViewAlert: (alert) => canViewAlert(user, alert),
    filterAlertsForUser: (alerts) => filterAlertsForUser(user, alerts),
    canViewServicePort: (clusterId, namespace, serviceName, port) =>
      canViewServicePort(user, clusterId, namespace, serviceName, port),
    getAllowedClusters: (clusters) => getAllowedClusters(user, clusters),
    getAllowedNamespaces: (clusterId, namespaces) =>
      getAllowedNamespaces(user, clusterId, namespaces),
    getAllowedResources: (clusterId, namespace, resources) =>
      getAllowedResources(user, clusterId, namespace, resources),
    getLogVisiblePods: (clusterId, namespace, pods) =>
      getLogVisiblePods(user, clusterId, namespace, pods),
    getProfileClusterIds: () => getProfileClusterIds(user),
    hasAnyClusterAccess: () => hasAnyClusterAccess(user),
    hasAnyNamespaceAccess: () => hasAnyNamespaceAccess(user),
    canAccessResourcesPage: () => canAccessResourcesPage(user),
    canAccessLogsPage: () => canAccessLogsPage(user),
    canViewResourceTab: (tabKey) => canViewResourceTab(user, tabKey),
    getVisibleResourceTabs: () => getVisibleResourceTabs(user),
    hasGrantedAction: (actionId) => hasGrantedAction(user, actionId),
    getGrantedActionIds: () => getGrantedActionIds(user),
    pageNeedsClusterContext,
    pageNeedsNamespaceContext,
    pageAllowed: (pageKey) => pageAllowed(user, pageKey),
    getVisiblePages: () => getVisiblePages(user),
    getFirstAllowedPage: () => getFirstAllowedPage(user),
    isAccessDeniedError,
    formatAccessError: (message, options) => formatAccessError(message, options),
    shouldShowAccessError: (message, options) => shouldShowAccessError(message, options),
  };
}
