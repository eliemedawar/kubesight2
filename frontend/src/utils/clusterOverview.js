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
