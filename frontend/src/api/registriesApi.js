import { request } from "./client";

export const listRegistries = () => request("/api/registries");

export const createRegistry = (payload) =>
  request("/api/registries", { method: "POST", body: payload });

export const updateRegistry = (id, payload) =>
  request(`/api/registries/${id}`, { method: "PUT", body: payload });

export const deleteRegistry = (id) =>
  request(`/api/registries/${id}`, { method: "DELETE" });

export const testRegistry = (id) =>
  request(`/api/registries/${id}/test`, { method: "POST" });

// With a clusterId that has linked registries, the image is checked in THOSE
// registries (found in any one → available), exactly like the deploy gate.
export const checkImage = (image, clusterId) =>
  request("/api/registries/check-image", {
    method: "POST",
    body: clusterId ? { image, clusterId } : { image },
  });

// { clusterId: [registry ids] } for every cluster linked to a registry.
export const listClusterRegistryLinks = () => request("/api/registries/cluster-links");

export const setClusterRegistries = (clusterId, registryIds) =>
  request(`/api/registries/clusters/${encodeURIComponent(clusterId)}`, {
    method: "PUT",
    body: { registryIds },
  });
