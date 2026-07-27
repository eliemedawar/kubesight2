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

// --- Deploy automation (Jenkins router + per-ticket runs) ---
export const getJenkinsConfig = () => request("/api/zoho/jenkins");

export const updateJenkinsConfig = (payload) =>
  request("/api/zoho/jenkins", { method: "PUT", body: payload });

export const testJenkinsConnection = () =>
  request("/api/zoho/jenkins/test", { method: "POST" });

export const listAutomationRuns = (limit = 50) =>
  request("/api/zoho/automation/runs", { query: { limit } });

export const startAutomationRun = (ticketRecordId) =>
  request("/api/zoho/automation/runs", { method: "POST", body: { ticketRecordId } });

export const cancelAutomationRun = (runId) =>
  request(`/api/zoho/automation/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });

// --- Layout field editor ---
export const getZohoLayout = (fresh = false) =>
  request("/api/zoho/layout", { query: fresh ? { fresh: 1 } : undefined });

export const setZohoFieldOptions = (fieldId, payload) =>
  request(`/api/zoho/fields/${fieldId}/options`, { method: "PUT", body: payload });

export const updateZohoField = (fieldId, payload) =>
  request(`/api/zoho/fields/${fieldId}`, { method: "PATCH", body: payload });

export const deleteZohoField = (fieldId) =>
  request(`/api/zoho/fields/${fieldId}`, { method: "DELETE" });

export const createZohoField = (payload) =>
  request("/api/zoho/fields", { method: "POST", body: payload });

// --- Sections (Zoho only exposes these via a WHOLE-LAYOUT replace) ---
// Always dry-run first: planZohoLayout returns the exact body the backend would
// PATCH, plus a diff, so the operator confirms before the layout is rewritten.
// POST rather than GET — `request` de-dupes concurrent identical GETs, and the
// mutation list doesn't encode cleanly into a query string.
export const planZohoLayout = (payload) =>
  request("/api/zoho/layout/plan", { method: "POST", body: payload });

export const moveZohoFieldToSection = (fieldId, sectionName) =>
  request(`/api/zoho/fields/${fieldId}/section`, {
    method: "PUT",
    body: { sectionName },
  });

export const renameZohoSection = (sectionId, name) =>
  request(`/api/zoho/sections/${sectionId}`, { method: "PATCH", body: { name } });

// --- Text -> Picklist conversion ---
// Zoho cannot retype a field, so this creates a NEW one with a NEW cf_ api name.
// The GET is the dry run: it reports every KubeSight setting still keyed on the
// old name, which is the part that breaks silently.
export const planZohoFieldConversion = (fieldId) =>
  request(`/api/zoho/fields/${fieldId}/convert`);

export const convertZohoField = (fieldId, payload) =>
  request(`/api/zoho/fields/${fieldId}/convert`, { method: "POST", body: payload });

// --- Option-source bindings (this dropdown's options come from Kubernetes) ---
// The catalogue of source kinds plus every current binding, including the three
// the sync owns (flagged `locked`).
export const getZohoOptionSources = () => request("/api/zoho/option-sources");

// 404 = "no binding", which is a normal state, not an error to surface.
export const getZohoFieldBinding = async (fieldId) => {
  try {
    return await request(`/api/zoho/fields/${fieldId}/binding`);
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
};

export const setZohoFieldBinding = (fieldId, payload) =>
  request(`/api/zoho/fields/${fieldId}/binding`, { method: "PUT", body: payload });

export const deleteZohoFieldBinding = (fieldId) =>
  request(`/api/zoho/fields/${fieldId}/binding`, { method: "DELETE" });

// POST, not GET: it carries the unsaved binding being edited, and `request`
// de-dupes concurrent identical GETs (two drafts would collapse into one).
export const previewZohoFieldBinding = (fieldId, payload) =>
  request(`/api/zoho/fields/${fieldId}/binding/preview`, { method: "POST", body: payload });

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
