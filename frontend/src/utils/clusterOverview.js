/** Formatting for the Cluster Overview stat cards. */

// null/undefined means "not known" (e.g. kubectl may not list PVs) — show a
// dash, never a misleading 0.
const known = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
const gib = (value) => `${Number(value).toFixed(Number(value) >= 100 ? 0 : 1)} GiB`;

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
