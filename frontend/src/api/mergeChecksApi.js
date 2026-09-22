import { request } from "./client";

/**
 * Merge checks — the gate between a pull request and a merge.
 *
 * Two scopes, and they are genuinely different things:
 *
 *   policy   the installation's quality gate, edited in Settings by an admin.
 *   config   one service's webhook, checks and optional gate override, edited
 *            on the service's Merge Checks tab.
 *
 * A service either inherits the policy or overrides it; nothing merges the two
 * field by field, which is why `effectiveGate` comes back from the server
 * rather than being worked out in the browser.
 */

// --- The installation-wide quality gate ------------------------------------

export const getMergeCheckPolicy = () => request("/api/ci/merge-checks/policy");

export const updateMergeCheckPolicy = (payload) =>
  request("/api/ci/merge-checks/policy", { method: "PUT", body: payload });

// --- One service -----------------------------------------------------------

export const getServiceMergeChecks = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/merge-checks`);

export const saveServiceMergeChecks = (serviceId, payload) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/merge-checks`, {
    method: "PUT",
    body: payload,
  });

/**
 * Whether Bitbucket will actually refuse the merge on a failed check.
 *
 * Its own call because it goes out to Bitbucket: the tab renders and the form
 * saves whether or not this answers.
 */
export const getMergeCheckEnforcement = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/merge-checks/enforcement`);

/** The shared secret, in plaintext. Audited server-side on every reveal. */
export const revealMergeCheckSecret = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/merge-checks/secret`);

export const rotateMergeCheckSecret = (serviceId) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/merge-checks/secret`, {
    method: "POST",
  });

// --- Verdicts --------------------------------------------------------------

export const listMergeChecks = (serviceId, query = {}) =>
  request(`/api/ci/services/${encodeURIComponent(serviceId)}/merge-checks/runs`, {
    query,
  });

export const getMergeCheck = (checkId) =>
  request(`/api/ci/merge-checks/runs/${encodeURIComponent(checkId)}`);

/** Re-send an already-decided verdict. Never re-decides it. */
export const redeliverMergeCheck = (checkId) =>
  request(`/api/ci/merge-checks/runs/${encodeURIComponent(checkId)}/redeliver`, {
    method: "POST",
  });
