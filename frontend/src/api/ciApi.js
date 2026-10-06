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

// What a Deploy stage can point at: a namespace's deployments and containers,
// the cluster's approval rule, and whether the caller could authorize it.
export const getCiDeployTarget = (clusterId, namespace = "") =>
  request("/api/ci/deploy-targets", { query: { clusterId, namespace: namespace || undefined } });

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

// Test results and coverage parsed from the build's test-report and
// coverage-report artifacts: totals, failed cases, per-file entries.
export const getCiBuildTests = (id) =>
  request(`/api/ci/builds/${encodeURIComponent(id)}/tests`);

// Failed tests and coverage over the service's last `limit` builds that kept reports.
export const getCiServiceTestTrend = (serviceId, query = {}) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/test-trend`, { query });

export const cancelCiBuild = (id) =>
  request(`/api/ci/builds/${encodeURIComponent(id)}/cancel`, { method: "POST" });

export const retryCiBuild = (id) =>
  request(`/api/ci/builds/${encodeURIComponent(id)}/retry`, { method: "POST" });

// Answer an Approval stage. The stage decides who may (its named approvers
// and/or ci_builds:approve holders, never the build's starter unless allowed);
// a refusal arrives as a 403/409 with the reason. Returns the updated build.
export const approveCiBuildStage = (buildId, stageId, comment = "") =>
  request(
    `/api/ci/builds/${encodeURIComponent(buildId)}/stages/${encodeURIComponent(stageId)}/approve`,
    { method: "POST", body: { comment } }
  );

export const rejectCiBuildStage = (buildId, stageId, comment = "") =>
  request(
    `/api/ci/builds/${encodeURIComponent(buildId)}/stages/${encodeURIComponent(stageId)}/reject`,
    { method: "POST", body: { comment } }
  );

// Who an Approval stage can name: active users, and whether each already holds
// ci_builds:approve.
export const listCiApproverCandidates = () => request("/api/ci/approvers");

// What an App store upload stage can point at: registered mobile apps with
// their store readiness (never credentials), and whether the caller may
// authorize a target (admin-only, like publishing from Mobile Apps).
export const getCiStoreUploadTargets = (serviceId) =>
  request("/api/ci/store-upload-targets", { query: { serviceId: serviceId || undefined } });

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
// Code scan report — the PDF of a quality-gated stage's findings
// ---------------------------------------------------------------------------

const codeScanPath = (buildId, stageId) =>
  `/api/ci/builds/${encodeURIComponent(buildId)}/stages/${encodeURIComponent(stageId)}/code-scan`;

export const getCiCodeScan = (buildId, stageId) => request(codeScanPath(buildId, stageId));

export const downloadCiCodeScanReport = async (buildId, stageId) => {
  const { ticket } = await request(`${codeScanPath(buildId, stageId)}/report-ticket`, { method: "POST" });
  startDownload(`${codeScanPath(buildId, stageId)}/report`, ticket);
};

export const sendCiCodeScanReport = (buildId, stageId, { recipients, note }) =>
  request(`${codeScanPath(buildId, stageId)}/send`, {
    method: "POST",
    body: { recipients, note },
  });

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

// Which tools every service shares one cache of (keys from cache.shared.options).
export const setCiCacheShared = (shared) =>
  request("/api/ci/cache", { method: "PUT", body: { shared } });

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


// ---------------------------------------------------------------------------
// Schedules — cron-triggered builds. The server is the only cron evaluator:
// the form asks previewCiSchedule for the words and the next runs rather than
// computing them, so what it shows and what fires cannot disagree.
// ---------------------------------------------------------------------------

const schedulesPath = (serviceId) =>
  `/api/ci/services/${encodeURIComponent(serviceId)}/schedules`;

export const listCiSchedules = (serviceId) => request(schedulesPath(serviceId));

export const createCiSchedule = (serviceId, payload) =>
  request(schedulesPath(serviceId), { method: "POST", body: payload });

export const updateCiSchedule = (serviceId, scheduleId, payload) =>
  request(`${schedulesPath(serviceId)}/${encodeURIComponent(scheduleId)}`, {
    method: "PUT",
    body: payload,
  });

export const deleteCiSchedule = (serviceId, scheduleId) =>
  request(`${schedulesPath(serviceId)}/${encodeURIComponent(scheduleId)}`, { method: "DELETE" });

export const runCiScheduleNow = (serviceId, scheduleId) =>
  request(`${schedulesPath(serviceId)}/${encodeURIComponent(scheduleId)}/run`, { method: "POST" });

export const previewCiSchedule = ({ cron, timezone, count }) =>
  request("/api/ci/schedules/preview", { method: "POST", body: { cron, timezone, count } });

// ---------------------------------------------------------------------------
// Webhooks — a URL that starts a build when something calls it (a generic
// sender, or Bitbucket on push). Preview runs the server's own planner on a
// sample body, so what the form predicts is what a delivery does.
// ---------------------------------------------------------------------------

const webhooksPath = (serviceId) =>
  `/api/ci/services/${encodeURIComponent(serviceId)}/webhooks`;
const webhookPath = (serviceId, webhookId) =>
  `${webhooksPath(serviceId)}/${encodeURIComponent(webhookId)}`;

export const listCiWebhooks = (serviceId) => request(webhooksPath(serviceId));

export const createCiWebhook = (serviceId, payload) =>
  request(webhooksPath(serviceId), { method: "POST", body: payload });

export const updateCiWebhook = (serviceId, webhookId, payload) =>
  request(webhookPath(serviceId, webhookId), { method: "PUT", body: payload });

export const deleteCiWebhook = (serviceId, webhookId) =>
  request(webhookPath(serviceId, webhookId), { method: "DELETE" });

export const listCiWebhookDeliveries = (serviceId, webhookId) =>
  request(`${webhookPath(serviceId, webhookId)}/deliveries`);

export const revealCiWebhookSecret = (serviceId, webhookId) =>
  request(`${webhookPath(serviceId, webhookId)}/secret`);

export const rotateCiWebhookSecret = (serviceId, webhookId) =>
  request(`${webhookPath(serviceId, webhookId)}/secret`, { method: "POST" });

export const previewCiWebhook = (serviceId, webhookId, payload, event = "") =>
  request(`${webhookPath(serviceId, webhookId)}/preview`, { method: "POST", body: { payload, event } });

export const testCiWebhook = (serviceId, webhookId, payload) =>
  request(`${webhookPath(serviceId, webhookId)}/test`, { method: "POST", body: { payload } });

export const setupCiWebhookInSource = (serviceId, webhookId) =>
  request(`${webhookPath(serviceId, webhookId)}/setup`, { method: "POST" });

export const getCiWebhookSourceStatus = (serviceId, webhookId) =>
  request(`${webhookPath(serviceId, webhookId)}/source-status`);

// ---------------------------------------------------------------------------
// Pipelines outside services (the Pipelines page). Each one is stored as a
// service row of kind "pipeline", so once it exists it is edited, run and
// browsed through the ordinary service/pipeline/build calls above.
// ---------------------------------------------------------------------------

export const listSharedPipelines = (query = {}) => request("/api/ci/shared-pipelines", { query });

export const getSharedPipeline = (id) =>
  request(`/api/ci/shared-pipelines/${encodeURIComponent(id)}`);

export const createSharedPipeline = (payload) =>
  request("/api/ci/shared-pipelines", { method: "POST", body: payload });

/** A service's pipeline as an unsaved draft for a shared pipeline's editor. */
export const getSharedPipelineCopyFrom = (id, serviceId) =>
  request(
    `/api/ci/shared-pipelines/${encodeURIComponent(id)}/copy-from/${encodeURIComponent(serviceId)}`
  );

export const attachSharedPipeline = (serviceId, sharedPipelineId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/shared-pipeline`, {
    method: "POST",
    body: { sharedPipelineId },
  });

/** mode: "restore" (the service's own stages come back) or "copy". */
export const detachSharedPipeline = (serviceId, mode = "restore") =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/shared-pipeline/detach`, {
    method: "POST",
    body: { mode },
  });

// ---------------------------------------------------------------------------
// Deployment links — which inventory deployments a service builds
// ---------------------------------------------------------------------------

const linksPath = (serviceId) => `/api/ci/services/${encodeURIComponent(serviceId)}/deployments`;

export const listCiDeploymentLinks = (serviceId, { live = true } = {}) =>
  request(linksPath(serviceId), { query: { live: live ? "true" : "false" } });

export const createCiDeploymentLink = (serviceId, payload) =>
  request(linksPath(serviceId), { method: "POST", body: payload });

export const updateCiDeploymentLink = (serviceId, linkId, payload) =>
  request(`${linksPath(serviceId)}/${encodeURIComponent(linkId)}`, { method: "PUT", body: payload });

export const deleteCiDeploymentLink = (serviceId, linkId) =>
  request(`${linksPath(serviceId)}/${encodeURIComponent(linkId)}`, { method: "DELETE" });

// ---------------------------------------------------------------------------
// Inventory templates a Deploy stage or a deployment link creates from
// ---------------------------------------------------------------------------

export const listCiDeployTemplates = () => request("/api/ci/deploy-templates");

/** What a build would create from a template there — or why it cannot. */
export const previewCiDeployTemplate = (templateId, { namespace, deploymentName, containerName = "", answers }) =>
  request(`/api/ci/deploy-templates/${encodeURIComponent(templateId)}/preview`, {
    method: "POST",
    body: { namespace, deploymentName, containerName, answers },
  });
