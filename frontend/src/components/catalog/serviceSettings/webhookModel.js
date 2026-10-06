/**
 * The Webhooks section's pure half: the form ⇄ API translation, how a
 * delivery's outcome is written down, and the examples the page offers.
 *
 * Deliberately NOT here: deciding what a body builds. The server's planner is
 * the only one (services/ci/webhook_triggers.py) and the page asks it through
 * Preview, so what the form predicts is what a delivery does.
 */

export const KINDS = [
  { value: "bitbucket_push", label: "Build on push", hint: "Bitbucket repo:push" },
  { value: "generic", label: "Generic URL", hint: "Any tool that can POST" },
];

export const REF_TARGETS = [
  { value: "ref:branch", label: "Branch" },
  { value: "ref:tag", label: "Tag" },
  { value: "ref:commit", label: "Commit" },
];

const isRefTarget = (target) => REF_TARGETS.some((item) => item.value === target);

/** A new webhook's form. A push webhook starts filtered to the default branch:
 * building every feature-branch push is a choice somebody should make. */
export const blankWebhook = (kind = "bitbucket_push", defaultBranch = "main") => ({
  id: null,
  kind,
  name: kind === "bitbucket_push" ? "Build on push" : "Release tool",
  pipelineId: "",
  refType: "branch",
  branch: "",
  variables: {},
  allowedInputs: [],
  allowRefOverride: false,
  mappings: [],
  branchFilters: kind === "bitbucket_push" ? [defaultBranch || "main"] : [],
  buildTags: false,
  tagFilters: [],
  enabled: true,
  skipIfRunning: false,
});

/** An existing webhook as the form edits it. */
export const toForm = (hook) => ({
  id: hook.id,
  kind: hook.kind === "bitbucket_push" ? "bitbucket_push" : "generic",
  name: hook.name || "",
  pipelineId: hook.pipelineId ? String(hook.pipelineId) : "",
  refType: hook.refType === "tag" ? "tag" : "branch",
  branch: hook.branch || "",
  variables: { ...(hook.variables || {}) },
  allowedInputs: [...(hook.allowedInputs || [])],
  allowRefOverride: Boolean(hook.allowRefOverride),
  mappings: (hook.mappings || []).map((item) => ({ target: item.target || "", path: item.path || "" })),
  branchFilters: [...(hook.branchFilters || [])],
  buildTags: Boolean(hook.buildTags),
  tagFilters: [...(hook.tagFilters || [])],
  enabled: hook.enabled !== false,
  skipIfRunning: Boolean(hook.skipIfRunning),
});

/**
 * What the API is sent. Fixed values travel only for inputs the chosen
 * pipeline declares (a leftover from another pipeline would be refused for a
 * field the person cannot see), and empty mapping rows are dropped.
 */
export const toPayload = (form, parameters) => {
  const declared = new Set((parameters || []).map((param) => param.name));
  const variables = Object.fromEntries(
    Object.entries(form.variables || {}).filter(([name]) => declared.has(name))
  );
  const common = {
    name: String(form.name || "").trim(),
    pipelineId: form.pipelineId ? Number(form.pipelineId) : null,
    variables,
    branchFilters: [...(form.branchFilters || [])],
    tagFilters: [...(form.tagFilters || [])],
    enabled: Boolean(form.enabled),
    skipIfRunning: Boolean(form.skipIfRunning),
  };
  if (!form.id) common.kind = form.kind;
  if (form.kind === "bitbucket_push") {
    return { ...common, buildTags: Boolean(form.buildTags) };
  }
  const allowed = (form.allowedInputs || []).filter((name) => !declared.size || declared.has(name));
  return {
    ...common,
    refType: form.refType === "tag" ? "tag" : "branch",
    branch: String(form.branch || "").trim(),
    allowRefOverride: Boolean(form.allowRefOverride),
    allowedInputs: allowed,
    mappings: (form.mappings || [])
      .map((item) => ({ target: String(item.target || "").trim(), path: String(item.path || "").trim() }))
      .filter((item) => item.target || item.path),
  };
};

/** Local problems worth saying before the round-trip. The server still decides. */
export const formProblems = (form) => {
  const problems = {};
  if (!String(form.name || "").trim()) problems.name = "Give the webhook a name.";
  if (form.kind === "generic" && form.refType === "tag" && !String(form.branch || "").trim()) {
    problems.branch = "Name the tag — a tag has no default to fall back to.";
  }
  const mappings = (form.mappings || []).filter((item) => item.target || item.path);
  const broken = mappings.find((item) => !item.target || !item.path);
  if (broken) problems.mappings = "Each mapping needs both a target and a path.";
  const targets = mappings.map((item) => item.target).filter(Boolean);
  if (new Set(targets).size !== targets.length) problems.mappings = "A target is mapped twice.";
  if (targets.includes("ref:branch") && targets.includes("ref:tag")) {
    problems.mappings = "Map the branch or the tag, not both.";
  }
  return problems;
};

/** Does the form let the request (or a mapping) choose what is built? */
export const requestChoosesRef = (form) =>
  form.kind === "bitbucket_push" ||
  Boolean(form.allowRefOverride) ||
  (form.mappings || []).some((item) => item.target === "ref:branch" || item.target === "ref:tag");

export const targetLabel = (target) =>
  REF_TARGETS.find((item) => item.value === target)?.label || target;

/** "develop, release/*" / "any branch" — the push filter in words. */
export const filterWords = (patterns, noun = "branch") =>
  patterns && patterns.length ? patterns.join(", ") : `any ${noun}`;

/** One line on what a webhook builds. */
export const buildsWhat = (hook, defaultBranch) => {
  if (hook.kind === "bitbucket_push") {
    const branches = `Pushes to ${filterWords(hook.branchFilters)}`;
    return hook.buildTags ? `${branches}, and tags ${filterWords(hook.tagFilters, "tag")}` : branches;
  }
  const fixed =
    hook.refType === "tag"
      ? `tag ${hook.branch}`
      : hook.branch || `${defaultBranch || "main"} (default branch)`;
  return hook.allowRefOverride ? `${fixed}, or the ref the request names` : fixed;
};

const parseInstant = (iso) => {
  if (!iso) return NaN;
  const raw = String(iso).trim();
  return Date.parse(/(?:[Zz]|[+-]\d{2}:\d{2})$/.test(raw) ? raw : `${raw}Z`);
};

/** "5m ago", "3h ago", "2d ago". */
export const formatAgo = (iso, now = Date.now()) => {
  const at = parseInstant(iso);
  if (Number.isNaN(at)) return "";
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
};

const OUTCOME_TONES = {
  triggered: "ok",
  ignored: "warn",
  duplicate: "info",
  refused: "danger",
  failed: "danger",
};

const OUTCOME_LABELS = {
  triggered: "Built",
  ignored: "Ignored",
  duplicate: "Duplicate",
  refused: "Refused",
  failed: "Could not build",
};

export const deliveryTone = (outcome) => OUTCOME_TONES[outcome] || "unknown";
export const deliveryLabel = (outcome) => OUTCOME_LABELS[outcome] || "Unknown";

/** One line on the last delivery, and the tone to draw it in. */
export const outcomeOf = (hook) => {
  if (!hook.lastOutcome) return { tone: "unknown", label: "No calls yet", detail: "" };
  if (hook.lastOutcome === "triggered" && hook.lastBuild) {
    const status = hook.lastBuild.status;
    const tone =
      status === "success" ? "ok" : status === "failed" || status === "timeout" ? "danger" : status === "cancelled" ? "warn" : "info";
    return { tone, label: `Build #${hook.lastBuild.number} ${status || "queued"}`, detail: "" };
  }
  return {
    tone: deliveryTone(hook.lastOutcome),
    label: deliveryLabel(hook.lastOutcome),
    detail: hook.lastMessage || "",
  };
};

/** A call with the wrong secret since the last good one — the usual reason
 * "the sender says it delivered and nothing happened". */
export const rejectedRecently = (hook) => {
  const rejected = parseInstant(hook.lastRejectedAt);
  if (Number.isNaN(rejected)) return false;
  const delivered = parseInstant(hook.lastDeliveryAt);
  return Number.isNaN(delivered) || rejected > delivered;
};

/** What the rail says about the section while it is scrolled out of view. */
export const railSummary = (hooks) => {
  if (!hooks) return { text: "", tone: "" };
  if (!hooks.length) return { text: "None", tone: "" };
  const active = hooks.filter((item) => item.enabled).length;
  const broken = hooks.some(
    (item) =>
      item.enabled &&
      (item.pipelineProblem || ["failed", "refused"].includes(item.lastOutcome) || rejectedRecently(item))
  );
  return { text: active ? `${active} active` : "All off", tone: broken ? "warn" : "" };
};

/** "areeba/payment-service" from a Bitbucket URL, or "". */
export const repositoryName = (url) => {
  const match = String(url || "").match(/bitbucket\.org[/:]([^/\s]+)\/([^/\s?#]+?)(?:\.git)?\/?(?:[?#].*)?$/i);
  return match ? `${match[1]}/${match[2]}`.toLowerCase() : "";
};

/** The body Bitbucket sends for a push of one branch — for Preview and Test. */
export const samplePush = ({ repository = "", branch = "main", commit = "" } = {}) => ({
  repository: repository ? { full_name: repository } : {},
  push: {
    changes: [
      {
        new: {
          type: "branch",
          name: branch || "main",
          ...(commit ? { target: { hash: commit } } : {}),
        },
        old: null,
      },
    ],
  },
});

/** A body that exercises what a generic webhook allows, for Preview and curl. */
export const sampleBody = (hook, parameters = []) => {
  const body = {};
  if (hook.allowRefOverride) {
    if (hook.refType === "tag") body.tag = hook.branch || "v1.0.0";
    else body.branch = (hook.branchFilters || []).find((item) => !/[*?[]/.test(item)) || hook.branch || "main";
  }
  const allowed = hook.allowedInputs || [];
  if (allowed.length) {
    body.variables = Object.fromEntries(
      allowed.map((name) => {
        const param = (parameters || []).find((item) => item.name === name);
        const value =
          param?.type === "boolean"
            ? true
            : param?.type === "choice" && (param.choices || []).length
              ? param.choices[0]
              : name === "VERSION"
                ? "1.0.0"
                : "value";
        return [name, value];
      })
    );
  }
  return body;
};

const shellQuote = (text) => `'${String(text).replace(/'/g, `'\\''`)}'`;

/** A curl command that calls the webhook. The secret is a variable unless revealed. */
export const curlExample = (hook, { secret = "", body = {} } = {}) => {
  const lines = [
    `curl -fsS -X POST ${shellQuote(hook.url)}`,
    `  -H ${secret ? shellQuote(`X-KubeSight-Secret: ${secret}`) : `"X-KubeSight-Secret: $KUBESIGHT_WEBHOOK_SECRET"`}`,
  ];
  if (body && Object.keys(body).length) {
    lines.push(`  -H 'Content-Type: application/json'`);
    lines.push(`  -d ${shellQuote(JSON.stringify(body))}`);
  }
  return lines.join(" \\\n");
};

/** Mapping targets the form offers: the refs, then the pipeline's inputs. */
export const targetOptions = (parameters) => [
  ...REF_TARGETS,
  ...(parameters || []).map((param) => ({ value: param.name, label: param.label && param.label !== param.name ? `${param.name} — ${param.label}` : param.name })),
];

export { isRefTarget };
