import { request } from "./client";

// Level 1: the cluster with its namespaces (logical) and worker nodes (physical).
export const getClusterTopology = (clusterId) =>
  request(`/api/clusters/${encodeURIComponent(clusterId)}/topology`);

// Level 2: one namespace's pods, with Ingress → Service → pod edges.
export const getNamespaceTopology = (clusterId, namespace) =>
  request(
    `/api/clusters/${encodeURIComponent(clusterId)}/namespaces/${encodeURIComponent(
      namespace
    )}/topology`
  );
