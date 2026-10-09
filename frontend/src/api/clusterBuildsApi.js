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

// --- Day two: installing add-ons on a finished cluster ---------------------

/** Install catalog add-ons on the cluster; only the new ones are applied. */
export const addClusterBuildAddons = (id, addons) =>
  request(`/api/cluster-builds/${id}/addons`, { method: "POST", body: { addons } });

/** Withdraw a requested add-on that never installed (e.g. after a failure). */
export const removeClusterBuildAddon = (id, addonId) =>
  request(`/api/cluster-builds/${id}/addons/${encodeURIComponent(addonId)}`, {
    method: "DELETE",
  });

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

// --- SSH host keys (what makes strict / pinned usable) -----------------------

export const listSshHostKeys = () => request("/api/ssh-host-keys");

/** Fetch the fingerprint a host presents now. Trusts and records nothing. */
export const scanSshHostKey = ({ host, port = 22, profileId } = {}) =>
  request("/api/ssh-host-keys/scan", {
    method: "POST",
    body: { host, port, ...(profileId ? { profileId } : {}) },
  });

/** Pre-approve (pin) a fingerprint. `replace` must be true to overwrite a
 *  DIFFERENT recorded fingerprint (the server answers 409 otherwise). */
export const pinSshHostKey = ({ host, port = 22, keyType, fingerprint, replace = false }) =>
  request("/api/ssh-host-keys", {
    method: "POST",
    body: { host, port, keyType, fingerprint, replace },
  });

export const deleteSshHostKey = (id) =>
  request(`/api/ssh-host-keys/${id}`, { method: "DELETE" });

// --- Build profiles (repository modes) --------------------------------------

export const listBuildProfiles = () => request("/api/build-profiles");

export const createBuildProfile = (payload) =>
  request("/api/build-profiles", { method: "POST", body: payload });

export const updateBuildProfile = (id, payload) =>
  request(`/api/build-profiles/${id}`, { method: "PUT", body: payload });

export const deleteBuildProfile = (id) =>
  request(`/api/build-profiles/${id}`, { method: "DELETE" });

// --- Cluster templates ------------------------------------------------------

export const listClusterTemplates = () => request("/api/cluster-templates");

/** Save a template from a payload ({name, description, spec}) or a build ({name, fromBuildId}). */
export const createClusterTemplate = (payload) =>
  request("/api/cluster-templates", { method: "POST", body: payload });

export const updateClusterTemplate = (dbId, payload) =>
  request(`/api/cluster-templates/${dbId}`, { method: "PUT", body: payload });

export const deleteClusterTemplate = (dbId) =>
  request(`/api/cluster-templates/${dbId}`, { method: "DELETE" });

// --- OpenTofu provisioning --------------------------------------------------

/** Ask OpenTofu for a plan for this build's VMs. Returns the build (202); poll it. */
export const planClusterVms = (id) =>
  request(`/api/cluster-builds/${id}/provision/plan`, { method: "POST" });

export const planMoreWorkers = (id, payload) =>
  request(`/api/cluster-builds/${id}/provision/grow-plan`, { method: "POST", body: payload });

export const requestClusterDestroy = (id, payload) =>
  request(`/api/cluster-builds/${id}/provision/destroy`, { method: "POST", body: payload });

export const getProvisionJob = (id, jobId) =>
  request(`/api/cluster-builds/${id}/provision/jobs/${jobId}`);

/** The main.tf.json OpenTofu ran for a job: {filename, content}. No credentials in it. */
export const getProvisionJobConfig = (id, jobId) =>
  request(`/api/cluster-builds/${id}/provision/jobs/${jobId}/config`);

const jobAction = (id, jobId, action, body = {}) =>
  request(`/api/cluster-builds/${id}/provision/jobs/${jobId}/${action}`, { method: "POST", body });

export const applyProvisionPlan = (id, jobId) => jobAction(id, jobId, "apply");
export const approveClusterDestroy = (id, jobId, note = "") => jobAction(id, jobId, "approve", { note });
export const rejectClusterDestroy = (id, jobId, note = "") => jobAction(id, jobId, "reject", { note });
export const discardProvisionJob = (id, jobId) => jobAction(id, jobId, "discard");
export const retryProvisionConnect = (id, jobId) => jobAction(id, jobId, "retry-connect");

/** A VMs-only build whose VMs are ready: preflight them and build Kubernetes. */
export const installKubernetesOnVms = (id, payload = {}) =>
  request(`/api/cluster-builds/${id}/provision/install-kubernetes`, { method: "POST", body: payload });

export const getProvisioningOverview = () => request("/api/cluster-provisioning");

export const releaseProvisionLock = (buildId) =>
  request(`/api/cluster-provisioning/locks/${buildId}/release`, { method: "POST" });

// --- vCenter placement, networks, the provisioning account ------------------

export const getVSpherePlacement = (id, refresh = false) =>
  request(`/api/vsphere-connections/${id}/placement`, { query: refresh ? { refresh: 1 } : {} });

export const testVSphereProvisioning = (id) =>
  request(`/api/vsphere-connections/${id}/test-provisioning`, { method: "POST" });

export const listVSphereNetworks = (id) => request(`/api/vsphere-connections/${id}/networks`);

export const createVSphereNetwork = (id, payload) =>
  request(`/api/vsphere-connections/${id}/networks`, { method: "POST", body: payload });

export const updateVSphereNetwork = (id, rangeId, payload) =>
  request(`/api/vsphere-connections/${id}/networks/${rangeId}`, { method: "PUT", body: payload });

export const deleteVSphereNetwork = (id, rangeId) =>
  request(`/api/vsphere-connections/${id}/networks/${rangeId}`, { method: "DELETE" });

/** The next free addresses in a range — a preview, nothing is reserved. */
export const previewNetworkAddresses = (rangeId, count) =>
  request(`/api/vsphere-networks/${rangeId}/preview`, { query: { count } });
