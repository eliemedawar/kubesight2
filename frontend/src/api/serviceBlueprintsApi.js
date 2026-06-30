import { request } from "./client";

// ---------------------------------------------------------------------------
// Service blueprints (Service Catalog)
// ---------------------------------------------------------------------------

export const listServiceBlueprints = () => request("/api/service-blueprints");

export const getServiceBlueprint = (id) =>
  request(`/api/service-blueprints/${encodeURIComponent(id)}`);

export const createServiceBlueprint = (payload) =>
  request("/api/service-blueprints", { method: "POST", body: payload });

export const updateServiceBlueprint = (id, payload) =>
  request(`/api/service-blueprints/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteServiceBlueprint = (id) =>
  request(`/api/service-blueprints/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Deploy From Blueprint
// ---------------------------------------------------------------------------

// Pre-fill data for the deploy wizard: generated names, suggested namespace,
// kubesight.io labels, per-component recommended mapping, and missing values.
export const buildBlueprintDeployPlan = (id, target) =>
  request(`/api/service-blueprints/${encodeURIComponent(id)}/deploy-plan`, {
    method: "POST",
    body: target,
  });

// Persist the resolved choices -> creates an AppService + component mappings.
export const deployFromBlueprint = (id, payload) =>
  request(`/api/service-blueprints/${encodeURIComponent(id)}/deploy`, {
    method: "POST",
    body: payload,
  });

export const listBlueprintAppServices = (id) =>
  request(`/api/service-blueprints/${encodeURIComponent(id)}/app-services`);

// ---------------------------------------------------------------------------
// App services (blueprint instances) + runtime topology
// ---------------------------------------------------------------------------

export const listAppServices = (params = {}) => {
  const query = new URLSearchParams();
  if (params.clientId) query.set("clientId", params.clientId);
  if (params.blueprintId) query.set("blueprintId", params.blueprintId);
  const qs = query.toString();
  return request(`/api/app-services${qs ? `?${qs}` : ""}`);
};

export const getAppService = (id) =>
  request(`/api/app-services/${encodeURIComponent(id)}`);

export const deleteAppService = (id) =>
  request(`/api/app-services/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Live resource pickers (wizard) — degrade to empty lists off-cluster
// ---------------------------------------------------------------------------

export const pickNamespaces = (clusterId) =>
  request(`/api/service-blueprints/pickers/namespaces?clusterId=${encodeURIComponent(clusterId)}`);

export const pickNamespacedResources = (clusterId, namespace, kind, secretType) => {
  const query = new URLSearchParams({ clusterId, namespace, kind });
  if (secretType) query.set("secretType", secretType);
  return request(`/api/service-blueprints/pickers/resources?${query.toString()}`);
};

export const pickClusterResources = (clusterId, kind) =>
  request(
    `/api/service-blueprints/pickers/cluster-resources?clusterId=${encodeURIComponent(clusterId)}&kind=${encodeURIComponent(kind)}`
  );
