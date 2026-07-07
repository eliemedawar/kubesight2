import { request } from "./client";

export const getZohoConfig = () => request("/api/zoho/config");

export const updateZohoConfig = (payload) =>
  request("/api/zoho/config", { method: "PUT", body: payload });

export const testZohoConnection = () =>
  request("/api/zoho/test", { method: "POST" });

export const syncZohoNow = () => request("/api/zoho/sync", { method: "POST" });

// `fresh` (manual refresh) bypasses the server-side read caches.
export const getZohoPreview = (fresh = false) =>
  request("/api/zoho/preview", { query: fresh ? { fresh: 1 } : undefined });

export const listZohoInboundTickets = (limit = 50) =>
  request("/api/zoho/inbound-tickets", { query: { limit } });

export const deleteZohoInboundTicket = (recordId) =>
  request(`/api/zoho/inbound-tickets/${encodeURIComponent(recordId)}`, { method: "DELETE" });

// --- Layout field editor ---
export const getZohoLayout = (fresh = false) =>
  request("/api/zoho/layout", { query: fresh ? { fresh: 1 } : undefined });

export const setZohoFieldOptions = (fieldId, payload) =>
  request(`/api/zoho/fields/${fieldId}/options`, { method: "PUT", body: payload });

export const updateZohoField = (fieldId, payload) =>
  request(`/api/zoho/fields/${fieldId}`, { method: "PATCH", body: payload });

export const createZohoField = (payload) =>
  request("/api/zoho/fields", { method: "POST", body: payload });

// --- Dropdown source picker (cluster + namespaces -> live deployments) ---
export const getZohoSource = () => request("/api/zoho/source");

export const updateZohoSource = (payload) =>
  request("/api/zoho/source", { method: "PUT", body: payload });

export const getZohoSourceClusters = () => request("/api/zoho/source/clusters");

export const getZohoSourceNamespaces = (clusterId) =>
  request(`/api/zoho/source/clusters/${encodeURIComponent(clusterId)}/namespaces`);

export const getZohoSourceDeployments = (clusterId, namespaces = []) =>
  request("/api/zoho/source/deployments", {
    query: { clusterId, namespaces: namespaces.join(",") },
  });
