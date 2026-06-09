import { request } from "./client";

export function getHelmStatus() {
  return request("/api/helm/status");
}

export function listHelmReleases({ clusterId, namespace }) {
  return request("/api/helm/releases", { query: { clusterId, namespace } });
}

export function getHelmRelease({ clusterId, namespace, releaseName }) {
  return request(`/api/helm/releases/${encodeURIComponent(releaseName)}`, {
    query: { clusterId, namespace },
  });
}

export function listHelmRepos(clusterId) {
  return request("/api/helm/repos", { query: { clusterId } });
}

export function addHelmRepo(payload) {
  return request("/api/helm/repos", { method: "POST", body: payload });
}

export function searchHelmCharts({ clusterId, repo, q }) {
  return request("/api/helm/charts", { query: { clusterId, repo, q } });
}

export function renderHelmTemplate(payload) {
  return request("/api/helm/template", { method: "POST", body: payload });
}

export function dryRunHelmRelease(payload) {
  return request("/api/helm/dry-run", { method: "POST", body: payload });
}

export function installHelmRelease(payload) {
  return request("/api/helm/install", { method: "POST", body: payload });
}

export function upgradeHelmRelease(payload) {
  return request("/api/helm/upgrade", { method: "POST", body: payload });
}

export function rollbackHelmRelease(payload) {
  return request("/api/helm/rollback", { method: "POST", body: payload });
}

export function uninstallHelmRelease(payload) {
  return request("/api/helm/uninstall", { method: "POST", body: payload });
}

export function getHelmConfirmationPhrase(payload) {
  return request("/api/helm/confirmation-phrase", { method: "POST", body: payload });
}

export async function readChartArchiveAsBase64(file) {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  bytes.forEach((b) => {
    binary += String.fromCharCode(b);
  });
  return btoa(binary);
}
