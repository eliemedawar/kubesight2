/** Re-exports for cluster display helpers; grant state lives in accessRulesForm.js */

export {
  emptyClusterGrant as createEmptyClusterGrants,
  emptyClusterGrant,
} from "./accessRulesForm";

export function getClusterEnvironment(cluster) {
  if (!cluster) return "Unknown";
  const provider = cluster.provider || cluster.type;
  const region = cluster.region;
  if (provider && region) return `${String(provider).toUpperCase()} · ${region}`;
  if (provider) return String(provider);
  if (cluster.source === "custom") return "Custom cluster";
  return "Kubernetes";
}

export function clusterStatusTone(status) {
  const value = String(status || "").toLowerCase();
  if (value === "healthy" || value === "connected" || value === "ok") return "ok";
  if (value === "warning" || value === "warn" || value === "degraded") return "warn";
  if (value === "error" || value === "critical" || value === "danger") return "danger";
  return "info";
}
