import { hasAnyClusterAccess, hasAnyNamespaceAccess, isAdminUser } from "../utils/authz.js";

export function formatAlertTime(value) {
  if (!value) {
    return "—";
  }
  try {
    return new Date(value).toLocaleString();
  } catch {
    return String(value);
  }
}

export function getAlertResourceName(alert) {
  return alert?.pod || alert?.resourceName || alert?.resource || "—";
}

export function getAlertPolicyLabel(alert) {
  if (alert?.source === "alert_policy" && alert?.policyName) {
    return alert.policyName;
  }
  return alert?.policyName || "—";
}

export function getAlertTypeLabel(alert) {
  const type = alert?.alertType || "metric";
  if (type === "log") {
    return "Log";
  }
  if (type === "service") {
    return "Service";
  }
  if (type === "automation") {
    return "Automation";
  }
  return "Metric";
}

export function isLogAlert(alert) {
  return alert?.alertType === "log";
}

export function isServiceAlert(alert) {
  return alert?.alertType === "service";
}

export function isAutomationAlert(alert) {
  return alert?.alertType === "automation";
}

export function formatTriggeredConditions(alert) {
  if (isLogAlert(alert)) {
    return alert?.matchedPattern ? `Pattern: ${alert.matchedPattern}` : alert?.description || "—";
  }
  if (isServiceAlert(alert) || isAutomationAlert(alert)) {
    // Service/automation alert descriptions are already a full sentence naming
    // the service or ticket and what went wrong.
    return alert?.description || "—";
  }
  const conditions = alert?.triggeredConditions;
  if (!Array.isArray(conditions) || !conditions.length) {
    return alert?.description || "—";
  }
  return conditions
    .filter((item) => item?.matched !== false)
    .map((item) => {
      const label = item.metricLabel || item.metricKey || "metric";
      const observed = item.observedValue != null ? ` (observed ${item.observedValue})` : "";
      return `${label} ${item.operator || ""} ${item.threshold ?? ""}${observed}`.trim();
    })
    .join("; ");
}

export function buildAlertsScopeSummary({
  clusterId,
  clusters = [],
  namespaces = [],
  resources = {},
}) {
  const cluster = clusters.find((item) => item.id === clusterId);
  const namespaceNames = namespaces.map((ns) => ns?.name || ns).filter(Boolean);
  const resourceNames = (resources.pods || [])
    .map((pod) => pod?.name)
    .filter(Boolean)
    .sort();

  return {
    clusterLabel: cluster?.name || clusterId || "—",
    clusterId: clusterId || "",
    namespaces: namespaceNames,
    resources: resourceNames,
  };
}

export function hasAlertMonitoringScope({
  hasClusters,
  namespaces = [],
  resources = {},
  user,
}) {
  if (!hasClusters) {
    return false;
  }
  if ((resources.pods || []).length > 0) {
    return true;
  }
  if (namespaces.length > 0) {
    return true;
  }
  if (user && isAdminUser(user)) {
    return true;
  }
  if (user && (hasAnyNamespaceAccess(user) || hasAnyClusterAccess(user))) {
    return true;
  }
  return false;
}
