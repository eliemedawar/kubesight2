import { request } from "./client";

// ---------------------------------------------------------------------------
// Assisted CI configuration
//
// Analysis is a job, not a request: starting one returns a row to watch and the
// work happens on a worker. Everything here except `startCiAnalysis` is either
// reading that row or acting on its result.
// ---------------------------------------------------------------------------

/** Whether the assisted path can be offered at all, and why not if it cannot. */
export const getCiAssistAvailability = () => request("/api/ci/assist/availability");

/** The build environments, runner labels and limits a pipeline may use. */
export const getCiAssistCapabilities = () => request("/api/ci/assist/capabilities");

/** Start an analysis. Returns the queued row; poll getCiServiceAnalysis. */
export const startCiAnalysis = (serviceId, payload = {}) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/analysis`, {
    method: "POST",
    body: payload,
  });

/** The service's assisted-configuration state — the poll target. */
export const getCiServiceAnalysis = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/analysis`);

export const listCiAnalyses = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/analyses`);

export const getCiAnalysis = (analysisId) =>
  request(`/api/ci/analyses/${encodeURIComponent(analysisId)}`);

export const cancelCiAnalysis = (analysisId) =>
  request(`/api/ci/analyses/${encodeURIComponent(analysisId)}/cancel`, { method: "POST" });

/**
 * Save an approved proposal as a normal KubeSight pipeline.
 *
 * `inputs` carries the answers to the proposal's required inputs, including
 * secret values — which is why this is the one call in the file that must never
 * be logged or retried blindly.
 */
export const acceptCiAnalysis = (analysisId, { pipeline, applicationProfile, inputs } = {}) =>
  request(`/api/ci/analyses/${encodeURIComponent(analysisId)}/accept`, {
    method: "POST",
    body: { pipeline, applicationProfile, inputs },
  });

/** Correct what KubeSight believes the application is. */
export const updateCiApplicationProfile = (serviceId, payload) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/profile`, {
    method: "PUT",
    body: payload,
  });

/** Run a proposal past KubeSight's validator without saving it. */
export const validateCiGeneratedPipeline = (pipeline, serviceId, requiredInputs = []) =>
  request("/api/ci/pipelines/validate-generated", {
    method: "POST",
    body: { pipeline, serviceId, requiredInputs },
  });
