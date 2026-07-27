import { request } from "./client";

// The Ticketing tab talks to /api/ticketing/<provider>/… — one route surface over
// Zoho Desk and Jira. `makeTicketingApi(providerKey)` returns the same function
// names the tab has always called, bound to one provider, so every component
// below the picker stays provider-agnostic: it just calls `api.getConfig()`.
//
// The legacy /api/zoho/* endpoints still exist on the backend (the live Zoho Desk
// webhook is configured against one of them) but nothing in the UI calls them.

export const listTicketingProviders = () => request("/api/ticketing/providers");

export function makeTicketingApi(providerKey) {
  const base = `/api/ticketing/${encodeURIComponent(providerKey)}`;

  return {
    provider: providerKey,

    getConfig: () => request(`${base}/config`),

    updateConfig: (payload) => request(`${base}/config`, { method: "PUT", body: payload }),

    testConnection: () => request(`${base}/test`, { method: "POST" }),

    syncNow: () => request(`${base}/sync`, { method: "POST" }),

    // `fresh` (manual refresh) bypasses the server-side read caches.
    getPreview: (fresh = false) =>
      request(`${base}/preview`, { query: fresh ? { fresh: 1 } : undefined }),

    listInboundTickets: (limit = 50) => request(`${base}/inbound-tickets`, { query: { limit } }),

    deleteInboundTicket: (recordId) =>
      request(`${base}/inbound-tickets/${encodeURIComponent(recordId)}`, { method: "DELETE" }),

    // --- Deploy automation (Jenkins router + per-ticket runs) ---
    // Jenkins is a single shared connection: a build is a build whoever raised
    // the ticket. Both providers' tabs edit the same record.
    getJenkinsConfig: () => request(`${base}/jenkins`),

    updateJenkinsConfig: (payload) => request(`${base}/jenkins`, { method: "PUT", body: payload }),

    testJenkinsConnection: () => request(`${base}/jenkins/test`, { method: "POST" }),

    listAutomationRuns: (limit = 50) => request(`${base}/automation/runs`, { query: { limit } }),

    startAutomationRun: (ticketRecordId) =>
      request(`${base}/automation/runs`, { method: "POST", body: { ticketRecordId } }),

    cancelAutomationRun: (runId) =>
      request(`${base}/automation/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),

    // --- Form (layout / screen) field editor ---
    getLayout: (fresh = false) =>
      request(`${base}/layout`, { query: fresh ? { fresh: 1 } : undefined }),

    getField: (fieldId) => request(`${base}/fields/${fieldId}`),

    setFieldOptions: (fieldId, payload) =>
      request(`${base}/fields/${fieldId}/options`, { method: "PUT", body: payload }),

    updateField: (fieldId, payload) =>
      request(`${base}/fields/${fieldId}`, { method: "PATCH", body: payload }),

    // `payload` distinguishes "take off the form" from "delete the field" where
    // the provider draws that line (Jira does; Zoho Desk has no such split).
    deleteField: (fieldId, payload = {}) =>
      request(`${base}/fields/${fieldId}`, { method: "DELETE", body: payload }),

    createField: (payload) => request(`${base}/fields`, { method: "POST", body: payload }),

    // --- Sections ---
    // Zoho Desk only exposes section editing via a WHOLE-LAYOUT replace, so its
    // plan call returns the exact body that would be PATCHed and the UI confirms
    // it first. Jira applies each change on its own and its plan is advisory.
    // POST rather than GET — `request` de-dupes concurrent identical GETs, and
    // the mutation list doesn't encode cleanly into a query string.
    planLayout: (payload) => request(`${base}/layout/plan`, { method: "POST", body: payload }),

    listLayoutSnapshots: (limit = 10) =>
      request(`${base}/layout/snapshots`, { query: { limit } }),

    planLayoutSnapshotRestore: (snapshotId) =>
      request(`${base}/layout/snapshots/${encodeURIComponent(snapshotId)}/plan`, {
        method: "POST",
      }),

    restoreLayoutSnapshot: (snapshotId) =>
      request(`${base}/layout/snapshots/${encodeURIComponent(snapshotId)}/restore`, {
        method: "POST",
      }),

    createSection: (payload) => request(`${base}/sections`, { method: "POST", body: payload }),

    moveFieldToSection: (fieldId, sectionName) =>
      request(`${base}/fields/${fieldId}/section`, { method: "PUT", body: { sectionName } }),

    renameSection: (sectionId, name) =>
      request(`${base}/sections/${sectionId}`, { method: "PATCH", body: { name } }),

    // --- Text -> dropdown conversion (Zoho only; see capabilities.convertField) ---
    // Neither provider can retype a field, so this creates a NEW one with a NEW
    // api name. The GET is the dry run: it reports every KubeSight setting still
    // keyed on the old name, which is the part that breaks silently.
    planFieldConversion: (fieldId) => request(`${base}/fields/${fieldId}/convert`),

    convertField: (fieldId, payload) =>
      request(`${base}/fields/${fieldId}/convert`, { method: "POST", body: payload }),

    // --- Option-source bindings (this dropdown's options come from Kubernetes) ---
    // The catalogue of source kinds plus every current binding, including the
    // ones the sync owns (flagged `locked`).
    getOptionSources: () => request(`${base}/option-sources`),

    // 404 = "no binding", which is a normal state, not an error to surface.
    getFieldBinding: async (fieldId) => {
      try {
        return await request(`${base}/fields/${fieldId}/binding`);
      } catch (err) {
        if (err.status === 404) return null;
        throw err;
      }
    },

    setFieldBinding: (fieldId, payload) =>
      request(`${base}/fields/${fieldId}/binding`, { method: "PUT", body: payload }),

    deleteFieldBinding: (fieldId) =>
      request(`${base}/fields/${fieldId}/binding`, { method: "DELETE" }),

    // POST, not GET: it carries the unsaved binding being edited, and `request`
    // de-dupes concurrent identical GETs (two drafts would collapse into one).
    previewFieldBinding: (fieldId, payload) =>
      request(`${base}/fields/${fieldId}/binding/preview`, { method: "POST", body: payload }),

    // --- Dropdown source picker (cluster + namespaces -> live deployments) ---
    // One shared record across providers: it describes what KubeSight can deploy,
    // not what the ticketing system looks like.
    getSource: () => request(`${base}/source`),

    updateSource: (payload) => request(`${base}/source`, { method: "PUT", body: payload }),

    getSourceClusters: () => request(`${base}/source/clusters`),

    getSourceNamespaces: (clusterId) =>
      request(`${base}/source/clusters/${encodeURIComponent(clusterId)}/namespaces`),

    getSourceDeployments: (clusterId, namespaces = []) =>
      request(`${base}/source/deployments`, {
        query: { clusterId, namespaces: namespaces.join(",") },
      }),
  };
}
