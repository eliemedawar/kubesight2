/** Pure logic behind the Cluster Builder's Helm apps: Inventory charts that
 *  are installed once the cluster is up (wizard) or on day two.
 *
 *  The backend stays the authority — it checks the chart, the values file,
 *  the names and that every chart renders — but the editor answers the cheap
 *  questions (a missing chart, a bad name, a release twice) as you type.
 */

// The same shapes helm_service.validate_* enforce.
const NAMESPACE_RE = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const RELEASE_RE = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

/** What the backend shows in place of a stored password; sent back as-is,
 *  it keeps the stored value. */
export const VALUES_MASK = "<hidden>";

export const APP_STATUS_LABELS = {
  pending: "To install",
  installing: "Installing…",
  installed: "Installed",
  failed: "Failed",
  awaiting_approval: "Waiting for approval",
  rejected: "Not approved",
};

let nextKey = 0;
const newKey = () => `app-${Date.now().toString(36)}-${(nextKey += 1)}`;

/** A release name from a chart id: lowercase, hyphens, at most 53. */
export function releaseNameFor(slug) {
  return String(slug || "")
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 53)
    .replace(/-+$/g, "");
}

export function newAppItem(chart) {
  return {
    key: newKey(),
    chartSlug: chart?.id || "",
    chartVersion: chart?.version || "",
    valuesFile: "",
    namespace: "",
    releaseName: releaseNameFor(chart?.id),
    valuesYaml: "",
    valuesEdited: false,
  };
}

/** Editable items from a saved build: every chart not installed yet. */
export function appItemsFromBuild(build) {
  return (build?.apps || [])
    .filter((app) => app.status !== "installed")
    .map((app) => ({
      key: `saved-${app.id}`,
      id: app.id,
      chartSlug: app.chartSlug,
      chartVersion: app.chartVersion || "",
      valuesFile: app.valuesFile || "",
      namespace: app.namespace || "",
      releaseName: app.releaseName || "",
      valuesYaml: app.valuesYaml || "",
      // Saved values are the person's own: never overwritten by a reload.
      valuesEdited: true,
    }));
}

/** The request body for a list of items. */
export function appsPayload(items) {
  return (items || []).map((item) => ({
    ...(item.id ? { id: item.id } : {}),
    chartSlug: item.chartSlug,
    chartVersion: item.chartVersion || "",
    valuesFile: item.valuesFile || "",
    namespace: String(item.namespace || "").trim(),
    releaseName: String(item.releaseName || "").trim().toLowerCase(),
    valuesYaml: item.valuesYaml || "",
  }));
}

/** The first thing that would stop the list being saved, or "". */
export function appsError(items, installed = []) {
  const seen = new Set(
    (installed || []).map((app) => `${app.namespace}/${app.releaseName}`)
  );
  for (const [index, item] of (items || []).entries()) {
    const label = `Chart ${index + 1}`;
    if (!item.chartSlug) return `${label}: pick a Helm chart.`;
    const namespace = String(item.namespace || "").trim();
    if (!namespace) return `${label}: enter the namespace to install into.`;
    if (namespace.length > 63 || !NAMESPACE_RE.test(namespace)) {
      return `${label}: "${namespace}" is not a valid namespace name.`;
    }
    const release = String(item.releaseName || "").trim().toLowerCase();
    if (!release) return `${label}: enter a release name.`;
    if (release.length > 53 || !RELEASE_RE.test(release)) {
      return `${label}: "${release}" is not a valid release name (lowercase letters, digits, hyphens; at most 53).`;
    }
    const key = `${namespace}/${release}`;
    if (seen.has(key)) return `${label}: release ${release} is already in namespace ${namespace}.`;
    seen.add(key);
  }
  return "";
}

/** ``items`` with the one at ``index`` moved by ``delta`` (install order). */
export function moveItem(items, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  const [moved] = next.splice(index, 1);
  next.splice(target, 0, moved);
  return next;
}

/** Required inputs whose key does not appear with a value in the text.
 *  A hint only — there is no YAML parser here, and preflight is the check. */
export function unfilledRequired(valuesYaml, required = []) {
  const text = String(valuesYaml || "");
  return (required || []).filter((input) => {
    const key = String(input.path || "").split(".").pop();
    if (!key) return false;
    const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const line = new RegExp(`^\\s*${escaped}\\s*:\\s*(\\S.*)?$`, "m").exec(text);
    if (!line) return true;
    const value = (line[1] || "").trim();
    return value === "" || value === '""' || value === "''" || value === "~" || value === "null";
  });
}

/** One line per chart for summaries: "web → shop (web 1.2.0)". */
export function appSummary(app, chartName) {
  const name = chartName || app.chartName || app.chartSlug;
  return `${app.releaseName} → ${app.namespace} (${name}${app.chartVersion ? ` ${app.chartVersion}` : ""})`;
}
