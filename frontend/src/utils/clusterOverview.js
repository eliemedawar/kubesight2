/** Formatting for the Cluster Overview stat cards. */

// null/undefined means "not known" (e.g. kubectl may not list PVs) — show a
// dash, never a misleading 0.
const known = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
const gib = (value) => `${Number(value).toFixed(Number(value) >= 100 ? 0 : 1)} GiB`;

// 191 → "191", 12.5 → "12.5", 0.25 → "0.25": as many decimals as the size
// needs, no forced trailing zeros.
const amount = (value) => {
  const n = Number(value);
  const digits = Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 10 ? 1 : 2;
  return String(Number(n.toFixed(digits)));
};

/** "Healthy" from "healthy"; "Unknown" when the cluster reports none. */
export function statusValue(status) {
  const text = String(status || "").trim() || "unknown";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** "191 / 256 cores"; an unknown side is a dash, never 0. */
export function cpuValue(cpu) {
  if (!cpu || (!known(cpu.usedCores) && !known(cpu.capacityCores))) return "—";
  const side = (v) => (known(v) ? amount(v) : "—");
  return `${side(cpu.usedCores)} / ${side(cpu.capacityCores)} cores`;
}

/** "12.4 GiB / 64 GiB", in the same shape as the Storage card. */
export function memoryValue(memory) {
  if (!memory || (!known(memory.usedGiB) && !known(memory.capacityGiB))) return "—";
  const side = (v) => (known(v) ? gib(v) : "—");
  return `${side(memory.usedGiB)} / ${side(memory.capacityGiB)}`;
}

export function workloadsValue(workloads) {
  if (!workloads) return "—";
  const parts = [workloads.deployments, workloads.statefulsets, workloads.daemonsets];
  return parts.map((n) => (known(n) ? String(n) : "—")).join(" / ");
}

export function storageValue(storage) {
  if (!storage) return "—";
  const { usedGiB, capacityGiB, claimedGiB } = storage;
  if (known(usedGiB) && known(capacityGiB)) return `${gib(usedGiB)} / ${gib(capacityGiB)}`;
  if (known(claimedGiB) && known(capacityGiB)) return `${gib(claimedGiB)} / ${gib(capacityGiB)}`;
  if (known(capacityGiB)) return gib(capacityGiB);
  return "—";
}

export function storageDetail(storage) {
  if (!storage) return "Persistent volumes";
  if (known(storage.usedGiB)) return "Used / provisioned";
  if (known(storage.claimedGiB)) return "Claimed by PVCs / provisioned PVs";
  return "Provisioned persistent volumes";
}

// Cluster list statuses from the API are healthy / warning / unknown (plus
// critical / error); the same tone mapping on the Clusters cards and here.
const STATUS_TONES = {
  healthy: "ok",
  warning: "warn",
  critical: "danger",
  error: "danger",
  unknown: "info",
};

export const statusTone = (status) => STATUS_TONES[String(status).toLowerCase()] || "info";

/** Whole-number share of `capacity` that `used` is, or null when either is unknown. */
export function usagePercent(used, capacity) {
  if (!known(used) || !known(capacity) || Number(capacity) <= 0) return null;
  return Math.round((Number(used) / Number(capacity)) * 100);
}

/** A count for display: the number, or a dash when it is not known (never 0). */
export const countValue = (value) => (known(value) ? Number(value).toLocaleString() : "—");

/** "aws · us-east-1 · Kubernetes v1.30.2 · 18 nodes" from the fields the cluster has. */
export function clusterFacts(cluster) {
  if (!cluster) return "";
  const version = cluster.k8sVersion || cluster.version;
  const nodes = typeof cluster.nodes === "number" ? cluster.nodes : null;
  return [
    cluster.provider ? String(cluster.provider).toUpperCase() : null,
    cluster.region && cluster.region !== "unknown" ? cluster.region : null,
    version && version !== "unknown" ? `Kubernetes ${version}` : null,
    nodes != null ? `${nodes} node${nodes === 1 ? "" : "s"}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}
