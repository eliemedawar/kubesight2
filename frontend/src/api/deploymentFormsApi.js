import { getStoredToken } from "../authStorage";
import { getBaseUrl, request } from "./client";

function authHeaders() {
  const headers = {};
  const token = getStoredToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

function filenameFromDisposition(disposition, fallback) {
  if (!disposition) return fallback;
  const match = /filename="?([^"]+)"?/.exec(disposition);
  return match ? match[1] : fallback;
}

/**
 * Generate an .xlsx deployment form from a template and trigger a browser
 * download. Uses a raw fetch (not the JSON `request` helper) because the response
 * is a binary blob, not a JSON envelope.
 */
export async function generateDeploymentForm(templateId, { clusterId, namespace } = {}) {
  const response = await fetch(`${getBaseUrl()}/api/deployment-forms/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ templateId, clusterId, namespace }),
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      message = payload.error || payload.message || message;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(message === "Forbidden" ? "You do not have access to this resource." : message);
  }

  const blob = await response.blob();
  const filename = filenameFromDisposition(
    response.headers.get("Content-Disposition"),
    `deployment-form-${templateId}.xlsx`,
  );
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
  return { filename, formId: response.headers.get("X-Deployment-Form-Id") };
}

/**
 * Upload a filled .xlsx deployment form. Returns the parsed import record with
 * its answers + validation result. Uses multipart/form-data.
 */
export async function importDeploymentForm(file) {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${getBaseUrl()}/api/deployment-forms/import`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.success === false) {
    const message = payload.error || payload.message || `Request failed (${response.status})`;
    throw new Error(message === "Forbidden" ? "You do not have access to this resource." : message);
  }
  return payload.data;
}

export function getFormImport(importId) {
  return request(`/api/deployment-forms/imports/${importId}`);
}

export function validateFormImport(importId) {
  return request(`/api/deployment-forms/imports/${importId}/validate`, { method: "POST" });
}

export function applyImportToWizard(importId) {
  return request(`/api/deployment-forms/imports/${importId}/apply-to-wizard`, { method: "POST" });
}

export function addImportToBundle(importId) {
  return request(`/api/deployment-forms/imports/${importId}/add-to-bundle`, { method: "POST" });
}

export function sendImportForApproval(importId, payload = {}) {
  return request(`/api/deployment-forms/imports/${importId}/send-for-approval`, {
    method: "POST",
    body: payload,
  });
}
