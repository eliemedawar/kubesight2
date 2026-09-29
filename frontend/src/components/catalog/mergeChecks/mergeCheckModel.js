/**
 * The Merge Checks tab's model: is the gate really gating, which checks run,
 * and what counts as an unsaved change.
 *
 * Pure functions, so the four readiness answers the page leads with are the
 * ones the tests lock. The backend still decides everything — these only
 * read what it returned.
 */

import { TOOLS } from "../QualityGateFields.jsx";

/** Where a check comes from, so eleven tools read as four kinds of question. */
export const TOOL_CATEGORY = {
  eslint: "code",
  ruff: "code",
  pmd: "code",
  detekt: "code",
  swiftlint: "code",
  dart_analyze: "code",
  hadolint: "infra",
  shellcheck: "infra",
  semgrep: "security",
  sonar: "security",
  dependency_check: "dependencies",
};

export const CATEGORY_LABEL = {
  code: "Code lint",
  infra: "Dockerfiles & scripts",
  security: "Static analysis",
  dependencies: "Dependencies",
};

/** Two letters for a tool's badge — the name, not a logo we do not own. */
export const toolMonogram = (key) =>
  ({
    eslint: "ES",
    ruff: "Rf",
    pmd: "PM",
    detekt: "Dk",
    swiftlint: "Sw",
    dart_analyze: "Da",
    hadolint: "Hd",
    shellcheck: "Sh",
    semgrep: "Sg",
    sonar: "SQ",
    dependency_check: "DC",
  })[key] || String(key || "?").slice(0, 2);

export const toolLabel = (key) => TOOLS.find((tool) => tool.key === key)?.label || key;

export const EVENTS = [
  {
    value: "pullrequest:created",
    label: "A pull request is opened",
  },
  {
    value: "pullrequest:updated",
    label: "New commits are pushed to it",
    hint: "Keep this on — without it a fix never clears the gate.",
  },
  { value: "pullrequest:approved", label: "Somebody approves it" },
  { value: "pullrequest:fulfilled", label: "It is merged" },
];

/** The config as the form edits it. */
export function toForm(data) {
  return {
    enabled: Boolean(data?.enabled),
    tools: [...(data?.tools || [])],
    toolsMode: data?.toolsMode || "auto",
    events: [...(data?.events || [])],
    targetBranches: [...(data?.targetBranches || [])],
    statusKey: data?.statusKey || "KUBESIGH-MERGE",
    postComment: data?.postComment !== false,
    gateMode: data?.gateMode || "inherit",
    ...(data?.override || {}),
  };
}

const normalise = (value) =>
  Array.isArray(value) ? [...value].sort() : value === "" ? null : value;

/** The keys of the form that differ from the saved copy. `enabled` is left
 * out: the switch saves on its own, the moment it is flipped. */
export function dirtyKeys(form, saved) {
  if (!form || !saved) return [];
  const keys = new Set([...Object.keys(form), ...Object.keys(saved)]);
  keys.delete("enabled");
  return [...keys].filter(
    (key) => JSON.stringify(normalise(form[key])) !== JSON.stringify(normalise(saved[key]))
  );
}

/**
 * After a save that carried only part of the form (the switch, one script),
 * take the server's new copy without throwing away edits still in progress.
 * A key the person had changed keeps their value; every other key follows the
 * server.
 */
export function rebaseForm(form, oldSaved, newSaved) {
  const edited = new Set(dirtyKeys(form, oldSaved));
  const out = { ...newSaved };
  for (const key of edited) out[key] = form[key];
  return out;
}

/** The checks that will run on the next pull request. */
export function activeTools(config, form) {
  if (!config) return [];
  return form?.toolsMode === "custom" ? [...(form.tools || [])] : [...(config.recommendedTools || [])];
}

export function watchedBranches(config, service) {
  return config?.targetBranches?.length
    ? config.targetBranches
    : [service?.defaultBranch || "main"];
}

/** The gate is set but can never block: no total and no per-check cap. */
export function gateNeverBlocks(gate, tools) {
  if (!gate) return false;
  if (gate.maxTotalProblems !== null && gate.maxTotalProblems !== undefined) return false;
  return !TOOLS.filter((tool) => tools.includes(tool.key)).some(
    (tool) => gate[tool.capKey] !== null && gate[tool.capKey] !== undefined
  );
}

/**
 * The four things that all have to be true before a failed check stops a
 * merge. Each is answered from what the backend (and Bitbucket, through it)
 * said — "unknown" is a real answer and never shown as done.
 */
export function readiness({ config, webhook, enforcement, enabled }) {
  const steps = [];

  const canReport = Boolean(config?.sourceReady && config?.canReportVerdict?.ok);
  steps.push({
    key: "source",
    title: "Repository can take a verdict",
    state: canReport ? "ok" : "todo",
    detail: canReport
      ? "The service's credential can write build statuses."
      : !config?.sourceReady
        ? "Connect a repository and credential on the Source tab."
        : config?.canReportVerdict?.reason || "The credential cannot write build statuses.",
    // The credential is chosen on the Source tab, so both fixes land there.
    action: canReport ? null : !config?.sourceReady ? "source" : "credential",
  });

  let hook;
  if (!webhook) hook = { state: "checking", detail: "Asking Bitbucket…" };
  else if (!webhook.known) {
    hook = { state: "unknown", detail: webhook.reason || "Bitbucket could not be asked." };
  } else if (!webhook.exists) {
    hook = {
      state: "todo",
      detail: `No webhook in ${webhook.repository || "the repository"} points here yet.`,
      action: "setup",
    };
  } else if (!webhook.inSync) {
    const problems = [];
    if (!webhook.active) problems.push("it is disabled");
    if (!webhook.secretSet) problems.push("it has no secret");
    if (webhook.missingEvents?.length) problems.push(`it does not send ${webhook.missingEvents.join(", ")}`);
    hook = {
      state: "warn",
      detail: `The webhook exists, but ${problems.join(" and ") || "it is out of date"}.`,
      action: "setup",
    };
  } else {
    hook = { state: "ok", detail: `Set up in ${webhook.repository}.` };
  }
  steps.push({ key: "webhook", title: "Bitbucket sends pull requests here", ...hook });

  let gate;
  if (!enforcement) gate = { state: "checking", detail: "Asking Bitbucket…" };
  else if (!enforcement.known) {
    gate = {
      state: "unknown",
      detail: enforcement.reason || "The branch restrictions could not be read.",
    };
  } else if (enforcement.enforced && enforcement.hardBlock === false) {
    gate = {
      state: "warn",
      detail:
        "A failing check shows red but can still be merged — hard blocking needs Bitbucket Premium.",
    };
  } else if (enforcement.enforced) {
    const where = (enforcement.covered || []).join(", ");
    gate = { state: "ok", detail: `Bitbucket requires a passing check${where ? ` on ${where}` : ""}.` };
  } else {
    const missing = (enforcement.uncovered || []).join(", ");
    gate = {
      state: "todo",
      detail: `Nothing requires a passing build${missing ? ` on ${missing}` : ""}, so a blocked pull request can still be merged.`,
      action: "setup",
    };
  }
  steps.push({ key: "enforce", title: "A failed check stops the merge", ...gate });

  steps.push({
    key: "enabled",
    title: "Checks are switched on",
    state: enabled ? "ok" : "todo",
    detail: enabled
      ? "Every pull request into a watched branch is checked."
      : "Nothing is checked or reported while this is off.",
    action: enabled ? null : "enable",
  });

  const done = steps.filter((step) => step.state === "ok").length;
  // Stuck, not merely unprotected: Bitbucket waits for a status that will
  // never be posted, so every pull request into the branch is frozen.
  const stuck = !enabled && enforcement?.enforced;
  const overall = stuck ? "stuck" : done === steps.length ? "protected" : "partial";
  return { steps, done, total: steps.length, overall };
}

/** Per-tool findings of one pull request, in the order the tools are listed. */
export function runBreakdown(run) {
  const metrics = run?.metrics || {};
  const order = TOOLS.map((tool) => tool.key);
  return Object.entries(metrics)
    .map(([tool, value]) => ({
      tool,
      label: toolLabel(tool),
      status: value?.status || "missing",
      problems: Number(value?.problems) || 0,
      message: value?.message || "",
      cap: run?.gate?.[TOOLS.find((item) => item.key === tool)?.capKey],
    }))
    .sort((a, b) => order.indexOf(a.tool) - order.indexOf(b.tool));
}

/** The one metric line the gate reads. An edited script without it is read
 * as "not run" — said before saving, not after the next pull request. */
export const METRIC_MARKER = "##kubesight-metric";

/** The line that reports a real count is the `status=ok` one. The generated
 * scripts also print skipped/error lines for the same tool, so "mentions the
 * tool" alone would pass a script whose counting line was deleted. */
export const scriptKeepsMetric = (text, tool) =>
  String(text || "")
    .split("\n")
    .some(
      (line) =>
        line.includes(METRIC_MARKER) && line.includes(`tool=${tool}`) && line.includes("status=ok")
    );
