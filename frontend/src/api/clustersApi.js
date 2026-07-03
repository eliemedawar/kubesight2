import { request, streamSse } from "./client";

export const listClusters = () => request("/api/clusters");

export const listCustomClusters = () => request("/api/clusters/custom");

export const createCustomCluster = (payload) =>
  request("/api/clusters/custom", { method: "POST", body: payload });

export const updateCustomCluster = (clusterId, payload) =>
  request(`/api/clusters/custom/${encodeURIComponent(clusterId)}`, {
    method: "PUT",
    body: payload,
  });

export const deleteCustomCluster = (clusterId) =>
  request(`/api/clusters/custom/${encodeURIComponent(clusterId)}`, { method: "DELETE" });

export const testCustomCluster = (clusterId) =>
  request(`/api/clusters/custom/${encodeURIComponent(clusterId)}/test`, { method: "POST" });

export const getClusterOverview = (clusterId) =>
  request(`/api/clusters/${encodeURIComponent(clusterId)}/overview`);

export const listNamespacesByCluster = (clusterId, { lite = false, counts = false } = {}) =>
  request(`/api/clusters/${encodeURIComponent(clusterId)}/namespaces`, {
    query: lite ? { lite: 1 } : counts ? { counts: 1 } : undefined,
  });

// Per-namespace CPU/memory only — loaded in the background and merged onto rows
// that already show counts, so a slow/missing metrics-server can't blank them.
export const listNamespaceMetricsByCluster = (clusterId) =>
  request(`/api/clusters/${encodeURIComponent(clusterId)}/namespaces`, {
    query: { metrics: 1 },
  });

export const listStorageClasses = (clusterId) =>
  request(`/api/clusters/${encodeURIComponent(clusterId)}/storageclasses`);

export const listClusterNodes = (clusterId) =>
  request(`/api/clusters/${encodeURIComponent(clusterId)}/nodes`);

export const listNamespaceConfigResources = (clusterId, namespace) =>
  request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/config-resources`
  );

export const getResourcesByClusterNamespace = (clusterId, namespace, { fresh = false } = {}) =>
  request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/resources`,
    { query: fresh ? { fresh: 1 } : undefined }
  );

export const getResourceListByType = (clusterId, namespace, resourceType, { fresh = false } = {}) =>
  request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/resources/${encodeURIComponent(resourceType)}`,
    { query: fresh ? { fresh: 1 } : undefined }
  );

export const getClusterPodIssues = (clusterId) =>
  request(`/api/clusters/${encodeURIComponent(clusterId)}/pod-issues`);

export const getLogs = (query) => request("/api/logs", { query });

export function listNamespacePodsForLogs(clusterId, namespace) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/pods`
  );
}

export function listPodContainers(clusterId, namespace, podName) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(podName)}/containers`
  );
}

export function getContainerLogs(clusterId, namespace, podName, containerName, query = {}) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(podName)}/containers/${encodeURIComponent(containerName)}/logs`,
    { query }
  );
}

export function streamContainerLogs(
  clusterId,
  namespace,
  podName,
  containerName,
  { query = {}, signal, onEvent } = {}
) {
  return streamSse(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(podName)}/containers/${encodeURIComponent(containerName)}/logs/stream`,
    { query, signal, onEvent }
  );
}

export function getNamespaceEvents(clusterId, namespace, query = {}) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/events`,
    { query }
  );
}

export function getResourceDescribe({ clusterId, namespace, kind, name }) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/resources/${encodeURIComponent(kind)}/${encodeURIComponent(name)}/describe`
  );
}

export function getResourceYaml({ clusterId, namespace, kind, name }) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/resources/${encodeURIComponent(kind)}/${encodeURIComponent(name)}/yaml`
  );
}

export function restartResource({ clusterId, namespace, kind, name }) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/resources/${encodeURIComponent(kind)}/${encodeURIComponent(name)}/restart`,
    { method: "POST" }
  );
}

export function execInPod({ clusterId, namespace, podName, command, container }) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(podName)}/exec`,
    { method: "POST", body: { command, container: container || undefined } }
  );
}

export function getDeploymentRolloutHistory({ clusterId, namespace, deploymentName }) {
  return request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(namespace)}/deployments/${encodeURIComponent(deploymentName)}/rollout-history`
  );
}
