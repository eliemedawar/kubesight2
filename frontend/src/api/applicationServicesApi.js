import { request } from "./client";

export const listApplicationServices = () =>
  request("/api/application-services");

export const getApplicationService = (id) =>
  request(`/api/application-services/${encodeURIComponent(id)}`);

export const createApplicationService = (payload) =>
  request("/api/application-services", { method: "POST", body: payload });

export const updateApplicationService = (id, payload) =>
  request(`/api/application-services/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteApplicationService = (id) =>
  request(`/api/application-services/${encodeURIComponent(id)}`, { method: "DELETE" });

export const listPickerDeployments = (clusterId, namespace) =>
  request(
    `/api/application-services/picker/deployments?clusterId=${encodeURIComponent(clusterId)}&namespace=${encodeURIComponent(namespace)}`
  );

export const listPickerPods = (clusterId, namespace) =>
  request(
    `/api/application-services/picker/pods?clusterId=${encodeURIComponent(clusterId)}&namespace=${encodeURIComponent(namespace)}`
  );
