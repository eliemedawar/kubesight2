import { request } from "./client";

export function listInventory(query = {}) {
  return request("/api/inventory", { query });
}

export function getInventoryDetail(inventoryId) {
  // IDs from the API are already URL-encoded; encoding again breaks server matching.
  return request(`/api/inventory/${inventoryId}`);
}

export function listInventoryWorkloads({ clusterId, namespace }) {
  return request("/api/inventory/workloads", {
    query: { clusterId, namespace },
  });
}

export function registerExistingApp(payload) {
  return request("/api/inventory/register", { method: "POST", body: payload });
}

export function updateCatalogEntry(entryId, payload) {
  return request(`/api/inventory/${entryId}`, { method: "PUT", body: payload });
}

export function removeFromInventory(entryId) {
  return request(`/api/inventory/${entryId}`, { method: "DELETE" });
}

export function validateDeployYaml(payload) {
  return request("/api/inventory/deploy/yaml/validate", { method: "POST", body: payload });
}

export function dryRunDeployYaml(payload) {
  return request("/api/inventory/deploy/yaml/dry-run", { method: "POST", body: payload });
}

export function diffDeployYaml(payload) {
  return request("/api/inventory/deploy/yaml/diff", { method: "POST", body: payload });
}

export function applyDeployYaml(payload) {
  return request("/api/inventory/deploy/yaml/apply", { method: "POST", body: payload });
}

export function generateImageManifests(payload) {
  return request("/api/inventory/deploy/image/generate", { method: "POST", body: payload });
}

export function dryRunDeployImage(payload) {
  return request("/api/inventory/deploy/image/dry-run", { method: "POST", body: payload });
}

export function applyDeployImage(payload) {
  return request("/api/inventory/deploy/image/apply", { method: "POST", body: payload });
}

export function restartWorkload(payload) {
  return request("/api/inventory/actions/restart", { method: "POST", body: payload });
}

export function scaleWorkload(payload) {
  return request("/api/inventory/actions/scale", { method: "POST", body: payload });
}

export function rollbackWorkload(payload) {
  return request("/api/inventory/actions/rollback", { method: "POST", body: payload });
}

export function getRolloutHistory({ clusterId, namespace, workloadName }) {
  return request("/api/inventory/actions/rollout-history", {
    query: { clusterId, namespace, workloadName },
  });
}

export function listWizardTemplates() {
  return request("/api/inventory/deploy/wizard/templates");
}

export function getWizardTemplate(templateId) {
  return request(`/api/inventory/deploy/wizard/templates/${templateId}`);
}

export function resolveWizardTemplate(templateId, answers) {
  return request(`/api/inventory/deploy/wizard/templates/${templateId}/resolve`, {
    method: "POST",
    body: answers,
  });
}

export function createWizardTemplate(payload) {
  return request("/api/inventory/deploy/wizard/templates", { method: "POST", body: payload });
}

export function updateWizardTemplate(templateId, payload) {
  return request(`/api/inventory/deploy/wizard/templates/${templateId}`, { method: "PUT", body: payload });
}

export function deleteWizardTemplate(templateId) {
  return request(`/api/inventory/deploy/wizard/templates/${templateId}`, { method: "DELETE" });
}

export function importTemplatesFromYaml(yaml) {
  return request("/api/inventory/deploy/wizard/templates/import", {
    method: "POST",
    body: { yaml },
  });
}

export function deleteWizardTemplateCategory(category) {
  return request(`/api/inventory/deploy/wizard/templates/categories/${encodeURIComponent(category)}`, {
    method: "DELETE",
  });
}

export function validateWizardName(payload) {
  return request("/api/inventory/deploy/wizard/validate-name", { method: "POST", body: payload });
}

export function generateWizardManifests(payload) {
  return request("/api/inventory/deploy/wizard/generate", { method: "POST", body: payload });
}

export function validateWizardPrerequisites(payload) {
  return request("/api/inventory/deploy/wizard/validate-prerequisites", { method: "POST", body: payload });
}

export function dryRunWizardDeploy(payload) {
  return request("/api/inventory/deploy/wizard/dry-run", { method: "POST", body: payload });
}

export function diffWizardDeploy(payload) {
  return request("/api/inventory/deploy/wizard/diff", { method: "POST", body: payload });
}

export function applyWizardDeploy(payload) {
  return request("/api/inventory/deploy/wizard/apply", { method: "POST", body: payload });
}

export function listApplicationVersions(inventoryId) {
  return request(`/api/inventory/${inventoryId}/versions`);
}

export function getApplicationVersion(versionId, includeYaml = true) {
  return request(`/api/inventory/versions/${versionId}`, {
    query: { includeYaml: includeYaml ? "true" : "false" },
  });
}

export function compareApplicationVersions(versionA, versionB) {
  return request("/api/inventory/versions/compare", {
    query: { versionA, versionB },
  });
}

export function rollbackApplicationVersion(versionId, confirmation) {
  return request(`/api/inventory/versions/${versionId}/rollback`, {
    method: "POST",
    body: { confirmation },
  });
}
