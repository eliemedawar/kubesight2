import { request } from "./client";

// Upgrade Center. The fleet and a cluster's workspace are reads; readiness
// runs every check now and stores the result; a request is the plan that waits
// for approval and its window; pause / resume / stop work on a running job.

export const getUpgradeFleet = () => request("/api/upgrades/fleet");

export const getUpgradeWorkspace = (clusterId) =>
  request(`/api/upgrades/clusters/${encodeURIComponent(clusterId)}`);

export const runUpgradeReadiness = (clusterId, targetVersion) =>
  request(`/api/upgrades/clusters/${encodeURIComponent(clusterId)}/readiness`, {
    method: "POST",
    body: { targetVersion },
  });

export const getUpgradeRunbook = (clusterId, targetVersion) =>
  request(`/api/upgrades/clusters/${encodeURIComponent(clusterId)}/runbook`, {
    query: { targetVersion: targetVersion || undefined },
  });

export const startGuidedUpgrade = (clusterId, targetVersion) =>
  request(`/api/upgrades/clusters/${encodeURIComponent(clusterId)}/guided`, {
    method: "POST",
    body: { targetVersion },
  });

/** payload: { clusterId, targetVersion, options, schedule, waivers, confirmation, note } */
export const requestUpgrade = (payload) => request("/api/upgrades/requests", { method: "POST", body: payload });

export const approveUpgrade = (jobId, note) =>
  request(`/api/upgrades/requests/${encodeURIComponent(jobId)}/approve`, { method: "POST", body: { note } });

export const rejectUpgrade = (jobId, note) =>
  request(`/api/upgrades/requests/${encodeURIComponent(jobId)}/reject`, { method: "POST", body: { note } });

export const withdrawUpgrade = (jobId) =>
  request(`/api/upgrades/requests/${encodeURIComponent(jobId)}/withdraw`, { method: "POST" });

/** action: "pause" | "resume" | "stop" | "cancel-guided" */
export const controlUpgrade = (jobId, action) =>
  request(`/api/upgrades/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });

export const getUpgradeJob = (jobId) => request(`/api/upgrades/jobs/${encodeURIComponent(jobId)}`);

export const listUpgradeJobs = ({ clusterId, limit } = {}) =>
  request("/api/upgrades/jobs", { query: { clusterId: clusterId || undefined, limit: limit || undefined } });

// The single-cluster API the earlier page used; kept for the MCP tools and scripts.
export const getUpgradeInfo = (clusterId, targetVersion) =>
  request(
    `/api/upgrades/info?clusterId=${encodeURIComponent(clusterId)}&targetVersion=${encodeURIComponent(targetVersion)}`
  );

export const runUpgradePrecheck = (payload) =>
  request("/api/upgrades/precheck", { method: "POST", body: payload });

export const startUpgrade = (payload) =>
  request("/api/upgrades/start", { method: "POST", body: payload });
