import { request } from "./client";

export const listClients = () =>
  request("/api/clients");

export const getClient = (id) =>
  request(`/api/clients/${encodeURIComponent(id)}`);

export const createClient = (payload) =>
  request("/api/clients", { method: "POST", body: payload });

export const updateClient = (id, payload) =>
  request(`/api/clients/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteClient = (id) =>
  request(`/api/clients/${encodeURIComponent(id)}`, { method: "DELETE" });

// ─── Client Service Access Topology (client-specific connectivity overlays) ───

export const listTransportTypes = () =>
  request("/api/clients/transport-types");

export const listClientServices = (clientId) =>
  request(`/api/clients/${encodeURIComponent(clientId)}/services`);

export const saveClientServiceConnection = (clientId, serviceId, payload) =>
  request(
    `/api/clients/${encodeURIComponent(clientId)}/services/${encodeURIComponent(serviceId)}/connection`,
    { method: "POST", body: payload }
  );

export const getClientServiceTopology = (clientId, serviceId) =>
  request(
    `/api/clients/${encodeURIComponent(clientId)}/services/${encodeURIComponent(serviceId)}/topology`
  );

export const deleteClientServiceConnection = (clientId, serviceId, { deactivate = false } = {}) =>
  request(
    `/api/clients/${encodeURIComponent(clientId)}/services/${encodeURIComponent(serviceId)}/connection${deactivate ? "?deactivate=true" : ""}`,
    { method: "DELETE" }
  );
