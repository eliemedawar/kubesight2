import { getStoredToken } from "../authStorage";
import { getBaseUrl, request } from "./client";

// ── App CRUD ─────────────────────────────────────────────────────────
export const listMobileApps = () => request("/api/mobile-apps");

export const createMobileApp = (payload) =>
  request("/api/mobile-apps", { method: "POST", body: payload });

export const updateMobileApp = (id, payload) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteMobileApp = (id) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}`, { method: "DELETE" });

// ── Connection / credential tests (return {status, message}) ─────────
export const testMobileAppJenkins = (id) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}/test-jenkins`, { method: "POST" });

export const testMobileAppPlay = (id) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}/test-play`, { method: "POST" });

export const testMobileAppAppStore = (id) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}/test-appstore`, { method: "POST" });

// ── Builds ───────────────────────────────────────────────────────────
// Manual "Fetch latest build" — 202 with the ingested builds, 409 if the
// latest build was already ingested.
export const fetchMobileAppBuilds = (id) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}/fetch`, { method: "POST" });

export const listMobileAppBuilds = (id) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}/builds`);

export const deleteMobileBuild = (buildId) =>
  request(`/api/mobile-apps/builds/${encodeURIComponent(buildId)}`, { method: "DELETE" });

// ── Publishes (ADMIN only for the POST) ──────────────────────────────
export const listMobileAppPublishes = (id) =>
  request(`/api/mobile-apps/${encodeURIComponent(id)}/publishes`);

export const publishMobileBuild = (buildId, payload) =>
  request(`/api/mobile-apps/builds/${encodeURIComponent(buildId)}/publish`, {
    method: "POST",
    body: payload,
  });

// ── Binary download ──────────────────────────────────────────────────
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
 * Download a build's binary artifact and trigger a browser download. Uses a raw
 * fetch (not the JSON `request` helper) because the response is a binary blob,
 * not a JSON envelope.
 */
export async function downloadBuild(buildId, fallbackName = `build-${buildId}`) {
  const response = await fetch(
    `${getBaseUrl()}/api/mobile-apps/builds/${encodeURIComponent(buildId)}/download`,
    { headers: authHeaders() }
  );
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
    fallbackName
  );
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
  return { filename };
}
