import { request } from "./client";

export const listComponents = () => request("/api/topology-components");

export const getComponent = (id) =>
  request(`/api/topology-components/${encodeURIComponent(id)}`);

export const createComponent = (payload) =>
  request("/api/topology-components", { method: "POST", body: payload });

export const updateComponent = (id, payload) =>
  request(`/api/topology-components/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteComponent = (id) =>
  request(`/api/topology-components/${encodeURIComponent(id)}`, { method: "DELETE" });

export const checkComponentHealth = (id) =>
  request(`/api/topology-components/${encodeURIComponent(id)}/check`, { method: "POST" });
