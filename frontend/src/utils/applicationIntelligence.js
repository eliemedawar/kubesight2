export const TERMINAL_ANALYSIS_STATUSES = new Set([
  "Completed",
  "Completed With Warnings",
  "Failed",
  "Cancelled",
]);

export const SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Informational"];
export const CONFIDENCE_ORDER = ["Confirmed", "High", "Medium", "Low", "Informational"];
export const FINDING_STATUSES = [
  "Open",
  "Accepted",
  "Resolved",
  "False Positive",
  "Risk Accepted",
];

export function isAnalysisActive(status) {
  return Boolean(status) && !TERMINAL_ANALYSIS_STATUSES.has(status);
}

/**
 * Whether a run got far enough for its findings to mean anything.
 *
 * A failed or cancelled analysis has zero findings because it stopped, not
 * because the repository is clean, so its posture must never render as a
 * passing result.
 */
export function producedResult(status) {
  return status === "Completed" || status === "Completed With Warnings";
}

export function validateApplicationForm(form) {
  const errors = {};
  if (!String(form.name || "").trim()) errors.name = "Microservice name is required.";
  const repositoryUrl = String(form.repositoryUrl || "").trim();
  if (!/^https:\/\/(?:www\.)?bitbucket\.org\/[^/?#]+\/[^/?#]+(?:\.git)?$/i.test(repositoryUrl)) {
    errors.repositoryUrl = "Enter an HTTPS Bitbucket workspace/repository URL.";
  }
  if (!form.credentialProfileId) errors.credentialProfileId = "Select a read-only credential profile.";
  for (const [key, label] of [
    ["repositorySubdirectory", "Subdirectory"],
    ["dockerfilePath", "Dockerfile path"],
  ]) {
    const value = String(form[key] || "");
    if (value.startsWith("/") || value.includes("..") || value.includes("\\")) {
      errors[key] = `${label} must be a safe repository-relative path.`;
    }
  }
  return errors;
}

export function normalizeDropdownNames(response) {
  const source = response?.items || response?.namespaces || response || [];
  if (!Array.isArray(source)) return [];
  return [...new Set(
    source
      .map((item) => (typeof item === "string" ? item : item?.name))
      .map((value) => String(value || "").trim())
      .filter(Boolean)
  )].sort((left, right) => left.localeCompare(right));
}

/** Tone for a severity-derived risk level. Risk is never a 0-100 score. */
export function riskLevelTone(level) {
  if (level === "Critical" || level === "High") return "fail";
  if (level === "Medium") return "warning";
  if (level === "Low") return "pending";
  if (level === "None") return "pass";
  return "pending";
}

export function severityTone(severity) {
  if (severity === "Critical" || severity === "High") return "fail";
  if (severity === "Medium") return "warning";
  return "pending";
}

/**
 * Scanner coverage tone. "Hermes only" is a warning rather than a neutral
 * state: with no deterministic scanner, an empty dependency or CVE list means
 * "not measured", not "nothing found".
 */
export function coverageTone(label) {
  if (label === "Full") return "pass";
  if (label === "Partial") return "warning";
  return "fail";
}

export function severityRank(severity) {
  const index = SEVERITY_ORDER.indexOf(severity);
  return index === -1 ? SEVERITY_ORDER.length : index;
}

/** Highest severity first, then confirmed evidence, then stable by title. */
export function sortFindings(items) {
  return [...(items || [])].sort((left, right) => (
    severityRank(left.severity) - severityRank(right.severity)
    || CONFIDENCE_ORDER.indexOf(left.confidence) - CONFIDENCE_ORDER.indexOf(right.confidence)
    || String(left.title || "").localeCompare(String(right.title || ""))
  ));
}

export function formatTimestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

export function shortCommit(sha) {
  return sha ? String(sha).slice(0, 12) : null;
}

/** Turn a snake_case result key into a readable label. */
export function humanizeKey(key) {
  const text = String(key || "").replace(/[_-]+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

/**
 * Flatten one Hermes result object into label/value rows for display.
 * Nested objects and arrays are kept as structured values so no view has to
 * fall back to dumping raw JSON at the reader.
 */
export function toDetailRows(source) {
  if (!source || typeof source !== "object") return [];
  return Object.entries(source)
    .filter(([, value]) => (
      value !== null
      && value !== undefined
      && value !== ""
      && !(Array.isArray(value) && !value.length)
      && !(typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length)
    ))
    .map(([key, value]) => ({ key, label: humanizeKey(key), value }));
}
