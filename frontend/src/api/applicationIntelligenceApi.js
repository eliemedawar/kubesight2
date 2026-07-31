import { getStoredToken } from "../authStorage";
import { getBaseUrl, request } from "./client";

export const listIntelligenceApplications = (query) =>
  request("/api/applications", { query });

export const getIntelligenceApplication = (id) =>
  request(`/api/applications/${id}`);

export const createIntelligenceApplication = (body) =>
  request("/api/applications", { method: "POST", body });

export const updateIntelligenceApplication = (id, body) =>
  request(`/api/applications/${id}`, { method: "PATCH", body });

export const deleteIntelligenceApplication = (id) =>
  request(`/api/applications/${id}`, { method: "DELETE" });

export const requestApplicationAnalysis = (id, body) =>
  request(`/api/applications/${id}/analyses`, { method: "POST", body });

export const testHermesConnection = () =>
  request("/api/application-intelligence/hermes/test", { method: "POST" });

export const listBitbucketRepositoryRevisions = (body) =>
  request("/api/application-intelligence/bitbucket/revisions", {
    method: "POST",
    body,
  });

export const listBitbucketRepositoryDockerfiles = (body) =>
  request("/api/application-intelligence/bitbucket/dockerfiles", {
    method: "POST",
    body,
  });

export const getApplicationAnalysis = (id) =>
  request(`/api/application-analyses/${id}`);

export const cancelApplicationAnalysis = (id) =>
  request(`/api/application-analyses/${id}/cancel`, { method: "POST" });

export const listApplicationFindings = (id, query) =>
  request(`/api/application-analyses/${id}/findings`, { query });

export const updateApplicationFinding = (id, body) =>
  request(`/api/application-findings/${id}`, { method: "PATCH", body });

export const compareApplicationAnalyses = (id, baselineAnalysisId) =>
  request(`/api/application-analyses/${id}/compare`, {
    query: { baselineAnalysisId },
  });

export const listApplicationPullRequests = (id) =>
  request(`/api/application-analyses/${id}/pull-requests`);

export const createApplicationPullRequest = (id, body) =>
  request(`/api/application-analyses/${id}/pull-requests`, {
    method: "POST",
    body,
  });

export const getApplicationTopology = (id) =>
  request(`/api/application-analyses/${id}/topology`);

export const getApplicationApis = (id) =>
  request(`/api/application-analyses/${id}/apis`);

export const getApplicationConfiguration = (id) =>
  request(`/api/application-analyses/${id}/configuration`);

export const getApplicationRuntime = (id) =>
  request(`/api/application-analyses/${id}/runtime`);

export const collectApplicationRuntime = (id) =>
  request(`/api/application-analyses/${id}/runtime/collect`, { method: "POST" });

export const listApplicationArtifacts = (id) =>
  request(`/api/application-analyses/${id}/artifacts`);

export async function downloadApplicationArtifact(artifact) {
  const response = await fetch(
    `${getBaseUrl()}/api/application-artifacts/${artifact.id}/download`,
    { headers: { Authorization: `Bearer ${getStoredToken()}` } }
  );
  if (!response.ok) throw new Error("Artifact download failed.");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = artifact.filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function downloadApplicationNetworkPolicy(analysisId) {
  const response = await fetch(
    `${getBaseUrl()}/api/application-analyses/${analysisId}/runtime/network-policy`,
    { headers: { Authorization: `Bearer ${getStoredToken()}` } }
  );
  if (!response.ok) throw new Error("NetworkPolicy recommendation download failed.");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `network-policy-recommendation-${analysisId}.yaml`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function downloadFindingPatch(finding) {
  const response = await fetch(
    `${getBaseUrl()}/api/application-findings/${finding.id}/patch`,
    { headers: { Authorization: `Bearer ${getStoredToken()}` } }
  );
  if (!response.ok) throw new Error("Suggested patch download failed.");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `finding-${finding.id}.patch`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export const listBitbucketCredentialProfiles = () =>
  request("/api/bitbucket-credential-profiles");

export const createBitbucketCredentialProfile = (body) =>
  request("/api/bitbucket-credential-profiles", { method: "POST", body });

export const updateBitbucketCredentialProfile = (id, body) =>
  request(`/api/bitbucket-credential-profiles/${id}`, { method: "PATCH", body });

export const deleteBitbucketCredentialProfile = (id) =>
  request(`/api/bitbucket-credential-profiles/${id}`, { method: "DELETE" });
