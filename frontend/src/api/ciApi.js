import { getBaseUrl, request } from "./client";

/**
 * Hand a file to the browser's own download manager.
 *
 * Not `fetch` + blob: a CI artifact is routinely a 200MB JAR, and buffering one
 * in memory to hand it straight back to the disk costs the memory, loses the
 * progress bar and cannot resume a dropped transfer. A navigation to a URL that
 * answers with `Content-Disposition: attachment` streams instead.
 *
 * The cost of that choice is authentication: a navigation sends no headers, so
 * the credential has to travel in the URL. That is what the ticket is — a
 * two-minute token good for this one resource and nothing else (see
 * `auth_utils.create_download_ticket`).
 *
 * The anchor is synthetic rather than `window.location` so the current page is
 * never navigated away from if the response turns out not to be an attachment.
 */
const startDownload = (path, ticket) => {
  const url = `${getBaseUrl()}${path}?ticket=${encodeURIComponent(ticket)}`;
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.rel = "noopener";
  // No `download` attribute: it would override the filename the server sends in
  // Content-Disposition, and the server is the one that knows it.
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
};

// ---------------------------------------------------------------------------
// Services — the CI Service Catalog
// ---------------------------------------------------------------------------

export const listCiServices = (query = {}) => request("/api/ci/services", { query });

export const getCiService = (id) => request(`/api/ci/services/${encodeURIComponent(id)}`);

export const getCiServiceSummary = (id) =>
  request(`/api/ci/services/${encodeURIComponent(id)}/summary`);

export const createCiService = (payload) =>
  request("/api/ci/services", { method: "POST", body: payload });

export const updateCiService = (id, payload) =>
  request(`/api/ci/services/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteCiService = (id) =>
  request(`/api/ci/services/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Source
// ---------------------------------------------------------------------------

export const updateCiSource = (id, payload) =>
  request(`/api/ci/services/${encodeURIComponent(id)}/source`, {
    method: "PUT",
    body: payload,
  });

// Verifies the credential can actually read the repository. Returns
// { ok, message } either way — a failed probe is a result, not an error.
export const testCiSource = (id) =>
  request(`/api/ci/services/${encodeURIComponent(id)}/source/test`, { method: "POST" });

export const listCiBranches = (id) =>
  request(`/api/ci/services/${encodeURIComponent(id)}/source/branches`);

/**
 * Revisions for a repository the catalog has no service row for yet.
 *
 * `kinds` says which to fetch — ["branch"] or ["tag"]. A picker showing one
 * should not wait for the other: a repository with 450 tags costs several
 * seconds of listing that a branch dropdown never displays.
 */
export const previewCiRevisions = (payload) =>
  request("/api/ci/source/revisions", { method: "POST", body: payload });

export const listCiSourceCredentials = () => request("/api/ci/source/credentials");

export const createCiSourceCredential = (payload) =>
  request("/api/ci/source/credentials", { method: "POST", body: payload });

export const updateCiSourceCredential = (id, payload) =>
  request(`/api/ci/source/credentials/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: payload,
  });

export const deleteCiSourceCredential = (id) =>
  request(`/api/ci/source/credentials/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Pipelines
// ---------------------------------------------------------------------------

export const listCiPipelines = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/pipelines`);

export const getCiPipeline = (id) => request(`/api/ci/pipelines/${encodeURIComponent(id)}`);

export const createCiPipeline = (serviceId, payload) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/pipelines`, {
    method: "POST",
    body: payload,
  });

// Full replace, including the ordered stage list — reordering is one request.
export const updateCiPipeline = (id, payload) =>
  request(`/api/ci/pipelines/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteCiPipeline = (id) =>
  request(`/api/ci/pipelines/${encodeURIComponent(id)}`, { method: "DELETE" });

export const listCiPipelineTemplates = () => request("/api/ci/pipeline-templates");

// One file out of the service's repository, read without opening Bitbucket. A
// missing file arrives as a 400 with a readable message.
export const readCiSourceFile = (serviceId, path, revision = "") =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/source/file`, {
    method: "POST",
    body: { path, revision },
  });

// Read a Jenkinsfile into a pipeline draft. Writes nothing: the draft comes
// back as stages and parameters for the editor to hold as unsaved changes, so
// a translation is reviewed before it replaces a pipeline that works. Passing
// serviceId is what lets the backend check credential bindings against the
// secrets that service actually has.
export const importCiJenkinsfile = (content, serviceId) =>
  request("/api/ci/pipelines/import/jenkinsfile", {
    method: "POST",
    body: { content, serviceId },
  });

export const applyCiPipelineTemplate = (serviceId, applicationType) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/pipelines/from-template`, {
    method: "POST",
    body: { applicationType },
  });

// ---------------------------------------------------------------------------
// Builds
// ---------------------------------------------------------------------------

// What Run Build must ask for, with dynamic choices already resolved.
export const getCiServiceParameters = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/parameters`);


// One directory of a running build's shared workspace. Live only: the
// workspace goes away with the build pod, and the API says so rather than
// returning an empty listing.
export const getCiBuildWorkspace = (buildId, path) =>
  request(`/api/ci/builds/${encodeURIComponent(buildId)}/workspace`, {
    query: path ? { path } : {},
  });

export const listCiBuilds = (query = {}) => request("/api/ci/builds", { query });

export const listCiServiceBuilds = (serviceId, query = {}) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/builds`, { query });

// The Builds tab's Stages view: one grid of the recent history, aligned by
// stage name, with per-stage averages the client does not have to compute.
export const getCiStageMatrix = (serviceId, query = {}) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/stage-matrix`, { query });

export const runCiBuild = (serviceId, payload = {}) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/builds`, {
    method: "POST",
    body: payload,
  });

export const getCiBuild = (id) => request(`/api/ci/builds/${encodeURIComponent(id)}`);

export const cancelCiBuild = (id) =>
  request(`/api/ci/builds/${encodeURIComponent(id)}/cancel`, { method: "POST" });

export const retryCiBuild = (id) =>
  request(`/api/ci/builds/${encodeURIComponent(id)}/retry`, { method: "POST" });

// Offset read: pass the previous response's nextSeq to fetch only new lines.
export const getCiStageLogs = (buildId, stageId, after = 0, limit = 1000) =>
  request(
    `/api/ci/builds/${encodeURIComponent(buildId)}/stages/${encodeURIComponent(stageId)}/logs`,
    { query: { after, limit } }
  );

export const ciStageLogDownloadPath = (buildId, stageId) =>
  `/api/ci/builds/${encodeURIComponent(buildId)}/stages/${encodeURIComponent(stageId)}/logs/download`;

export const createCiStageLogDownloadTicket = (buildId, stageId) =>
  request(
    `/api/ci/builds/${encodeURIComponent(buildId)}/stages/${encodeURIComponent(
      stageId
    )}/logs/download-ticket`,
    { method: "POST" }
  );

export const downloadCiStageLog = async (buildId, stageId) => {
  const { ticket } = await createCiStageLogDownloadTicket(buildId, stageId);
  startDownload(ciStageLogDownloadPath(buildId, stageId), ticket);
};

// ---------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------

export const listCiServiceArtifacts = (serviceId, query = {}) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/artifacts`, { query });

export const listCiBuildArtifacts = (buildId) =>
  request(`/api/ci/builds/${encodeURIComponent(buildId)}/artifacts`);

export const getCiArtifact = (id) => request(`/api/ci/artifacts/${encodeURIComponent(id)}`);

export const ciArtifactDownloadPath = (id) =>
  `/api/ci/artifacts/${encodeURIComponent(id)}/download`;

export const createCiArtifactDownloadTicket = (id) =>
  request(`/api/ci/artifacts/${encodeURIComponent(id)}/download-ticket`, { method: "POST" });

export const downloadCiArtifact = async (id) => {
  const { ticket } = await createCiArtifactDownloadTicket(id);
  startDownload(ciArtifactDownloadPath(id), ticket);
};

// ---------------------------------------------------------------------------
// Secrets — values are write-only; reads return names and metadata only.
// ---------------------------------------------------------------------------

export const listCiSecrets = (serviceId) =>
  serviceId
    ? request(`/api/ci/services/${encodeURIComponent(serviceId)}/secrets`)
    : request("/api/ci/secrets");

export const createCiSecret = (serviceId, payload) =>
  serviceId
    ? request(`/api/ci/services/${encodeURIComponent(serviceId)}/secrets`, {
        method: "POST",
        body: payload,
      })
    : request("/api/ci/secrets", { method: "POST", body: payload });

export const updateCiSecret = (id, payload) =>
  request(`/api/ci/secrets/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

export const deleteCiSecret = (id) =>
  request(`/api/ci/secrets/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Runners
// ---------------------------------------------------------------------------

export const listCiRunners = () => request("/api/ci/runners");

export const getCiRunner = (id) => request(`/api/ci/runners/${encodeURIComponent(id)}`);

// Register an agent. The response carries the plaintext token exactly once.
export const registerCiAgent = (payload) =>
  request("/api/ci/runners/agents", { method: "POST", body: payload });

// Issue a new token; the old one stops working immediately.
export const rotateCiAgentToken = (id) =>
  request(`/api/ci/runners/${encodeURIComponent(id)}/token`, { method: "POST" });

export const deleteCiRunner = (id) =>
  request(`/api/ci/runners/${encodeURIComponent(id)}`, { method: "DELETE" });

export const updateCiRunner = (id, payload) =>
  request(`/api/ci/runners/${encodeURIComponent(id)}`, { method: "PUT", body: payload });

// ---------------------------------------------------------------------------
// Build cache — one shared volume every stage mounts at /cache.
// ---------------------------------------------------------------------------

export const getCiCache = () => request("/api/ci/cache");

// Turning it on requires a bound claim; the API refuses otherwise rather than
// letting every build fail at its first stage.
export const setCiCacheEnabled = (enabled) =>
  request("/api/ci/cache", { method: "PUT", body: { enabled } });

// Create-only. Needs the cluster-scoped grant in k8s/ci-cache-rbac.yaml,
// and says so if it is missing.
export const createCiCacheVolume = (payload) =>
  request("/api/ci/cache/volume", { method: "POST", body: payload });

// Both of these start a Job and return immediately; the outcome arrives in the
// `maintenance` block of the next getCiCache().
export const measureCiCache = () => request("/api/ci/cache/measure", { method: "POST" });

export const cleanCiCache = (payload) =>
  request("/api/ci/cache/clean", { method: "POST", body: payload });

// ---------------------------------------------------------------------------
// Artifact retention — artifacts expire on their own; these are the manual
// controls. Container images are never affected: their bytes are in a registry.
// ---------------------------------------------------------------------------

export const getCiArtifactPolicy = (serviceId) =>
  request("/api/ci/artifacts/policy", { query: serviceId ? { serviceId } : {} });

// Omit olderThanDays for the configured expiry; pass 0 with keepLast 0 to mean
// "everything in scope".
export const purgeCiArtifacts = (payload) =>
  request("/api/ci/artifacts/purge", { method: "POST", body: payload });

export const deleteCiArtifact = (id) =>
  request(`/api/ci/artifacts/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Runner portability — what a pipeline assumes about where it runs. Pure text
// analysis on the server, so it is cheap to call on every edit.
// ---------------------------------------------------------------------------

export const lintCiPipeline = (stages) =>
  request("/api/ci/pipelines/lint", { method: "POST", body: { stages } });

export const getCiPipelinePortability = (pipelineId) =>
  request(`/api/ci/pipelines/${encodeURIComponent(pipelineId)}/portability`);

