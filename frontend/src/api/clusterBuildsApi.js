import { request } from "./client";

// --- Cluster builds ---------------------------------------------------------

export const listClusterBuilds = () => request("/api/cluster-builds");

export const getClusterBuild = (id) => request(`/api/cluster-builds/${id}`);

export const getBuilderOptions = () => request("/api/cluster-builds/options");

export const createClusterBuild = (payload) =>
  request("/api/cluster-builds", { method: "POST", body: payload });

export const updateClusterBuild = (id, payload) =>
  request(`/api/cluster-builds/${id}`, { method: "PUT", body: payload });

export const deleteClusterBuild = (id) =>
  request(`/api/cluster-builds/${id}`, { method: "DELETE" });

export const preflightClusterBuild = (id) =>
  request(`/api/cluster-builds/${id}/preflight`, { method: "POST" });

export const startClusterBuild = (id, payload = {}) =>
  request(`/api/cluster-builds/${id}/start`, { method: "POST", body: payload });

export const retryClusterBuild = (id) =>
  request(`/api/cluster-builds/${id}/retry`, { method: "POST" });

export const cancelClusterBuild = (id) =>
  request(`/api/cluster-builds/${id}/cancel`, { method: "POST" });

export const getClusterBuildLogs = (id, nodeId) =>
  request(`/api/cluster-builds/${id}/logs`, { query: nodeId ? { node: nodeId } : {} });

// --- Day two: growing a finished cluster ------------------------------------

export const addClusterBuildNodes = (id, nodes) =>
  request(`/api/cluster-builds/${id}/nodes`, { method: "POST", body: { nodes } });

export const removeClusterBuildNode = (id, nodeId) =>
  request(`/api/cluster-builds/${id}/nodes/${nodeId}`, { method: "DELETE" });

export const preflightClusterGrowth = (id) =>
  request(`/api/cluster-builds/${id}/grow-preflight`, { method: "POST" });

export const growClusterBuild = (id, payload = {}) =>
  request(`/api/cluster-builds/${id}/grow`, { method: "POST", body: payload });

// --- Bringing workloads from an existing cluster ----------------------------

/** Clusters that can act as a source, plus the registries to check against. */
export const listWorkloadSources = () =>
  request("/api/cluster-builds/workload-sources");

export const listWorkloadNamespaces = (clusterId) =>
  request(
    `/api/cluster-builds/workload-sources/${encodeURIComponent(clusterId)}/namespaces`
  );

export const listWorkloadsInNamespace = (clusterId, namespace) =>
  request(
    `/api/cluster-builds/workload-sources/${encodeURIComponent(clusterId)}`
    + `/namespaces/${encodeURIComponent(namespace)}/workloads`
  );

/** What a selection would copy, and which images are missing from the registry.
 *  Answers before a build row exists, which is what the wizard needs. */
export const planWorkloadCopy = (selection) =>
  request("/api/cluster-builds/workload-plan", { method: "POST", body: selection });

export const setBuildWorkloads = (id, workloads) =>
  request(`/api/cluster-builds/${id}/workloads`, { method: "PUT", body: { workloads } });

export const getBuildWorkloadPlan = (id) =>
  request(`/api/cluster-builds/${id}/workload-plan`);

export const bringClusterWorkloads = (id, payload = {}) =>
  request(`/api/cluster-builds/${id}/bring-workloads`, { method: "POST", body: payload });

/** The cluster-admin kubeconfig. Every retrieval is audited server-side. */
export const getClusterBuildKubeconfig = (id) =>
  request(`/api/cluster-builds/${id}/kubeconfig`);

// --- vSphere connections ----------------------------------------------------

export const listVSphereConnections = () => request("/api/vsphere-connections");

export const createVSphereConnection = (payload) =>
  request("/api/vsphere-connections", { method: "POST", body: payload });

export const updateVSphereConnection = (id, payload) =>
  request(`/api/vsphere-connections/${id}`, { method: "PUT", body: payload });

export const deleteVSphereConnection = (id) =>
  request(`/api/vsphere-connections/${id}`, { method: "DELETE" });

export const testVSphereConnection = (id) =>
  request(`/api/vsphere-connections/${id}/test`, { method: "POST" });

export const listVSphereVms = (id, refresh = false) =>
  request(`/api/vsphere-connections/${id}/vms`, { query: refresh ? { refresh: 1 } : {} });

// --- SSH credentials + connection profiles ----------------------------------

export const listSshCredentials = () => request("/api/ssh-credentials");

export const createSshCredential = (payload) =>
  request("/api/ssh-credentials", { method: "POST", body: payload });

export const updateSshCredential = (id, payload) =>
  request(`/api/ssh-credentials/${id}`, { method: "PUT", body: payload });

export const deleteSshCredential = (id) =>
  request(`/api/ssh-credentials/${id}`, { method: "DELETE" });

export const listSshProfiles = () => request("/api/ssh-connection-profiles");

export const createSshProfile = (payload) =>
  request("/api/ssh-connection-profiles", { method: "POST", body: payload });

export const updateSshProfile = (id, payload) =>
  request(`/api/ssh-connection-profiles/${id}`, { method: "PUT", body: payload });

export const deleteSshProfile = (id) =>
  request(`/api/ssh-connection-profiles/${id}`, { method: "DELETE" });

export const testSshProfile = (id, host) =>
  request(`/api/ssh-connection-profiles/${id}/test`, { method: "POST", body: { host } });

// --- Build profiles (repository modes) --------------------------------------

export const listBuildProfiles = () => request("/api/build-profiles");

export const createBuildProfile = (payload) =>
  request("/api/build-profiles", { method: "POST", body: payload });

export const updateBuildProfile = (id, payload) =>
  request(`/api/build-profiles/${id}`, { method: "PUT", body: payload });

export const deleteBuildProfile = (id) =>
  request(`/api/build-profiles/${id}`, { method: "DELETE" });
