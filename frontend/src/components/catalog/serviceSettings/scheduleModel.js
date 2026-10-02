/**
 * The Schedules section's pure half: presets, the form ⇄ API translation, and
 * how a run time and an outcome are written down.
 *
 * Deliberately NOT here: any cron evaluation. The server owns the one cron
 * implementation (services/ci/cron.py) and the form asks it for the words and
 * the next runs, so what the page promises and what the engine fires cannot
 * drift apart. The presets below are matched by exact expression only.
 */

export const PRESETS = [
  { value: "0 2 * * *", label: "Nightly 02:00", hint: "Every day" },
  { value: "0 8 * * 1-5", label: "Weekdays 08:00", hint: "Mon–Fri" },
  { value: "0 * * * *", label: "Every hour", hint: "On the hour" },
  { value: "0 3 * * 0", label: "Weekly", hint: "Sunday 03:00" },
  { value: "custom", label: "Custom", hint: "Any cron" },
];

/** Which preset an expression is, or "custom". */
export const presetFor = (cron) => {
  const text = String(cron || "").trim().replace(/\s+/g, " ");
  return PRESETS.find((preset) => preset.value === text)?.value || "custom";
};

/** The viewer's IANA zone — the default for a new schedule. */
export const browserTimeZone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

/** Every zone the browser knows, for the picker. Always includes the ones in play. */
export const timeZoneOptions = (...extra) => {
  let zones = [];
  try {
    zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  } catch {
    zones = [];
  }
  const all = new Set(["UTC", ...zones, ...extra.filter(Boolean)]);
  return [...all].sort((a, b) => (a === "UTC" ? -1 : b === "UTC" ? 1 : a.localeCompare(b)));
};

/** A new schedule's form, prefilled for the most common case: a nightly. */
export const blankSchedule = ({ timezone = browserTimeZone(), name = "Nightly build" } = {}) => ({
  id: null,
  name,
  cron: "0 2 * * *",
  timezone,
  pipelineId: "",
  refType: "branch",
  branch: "",
  variables: {},
  enabled: true,
  skipIfRunning: true,
});

/** An existing schedule as the form edits it. */
export const toForm = (schedule) => ({
  id: schedule.id,
  name: schedule.name || "",
  cron: schedule.cron || "",
  timezone: schedule.timezone || "UTC",
  pipelineId: schedule.pipelineId ? String(schedule.pipelineId) : "",
  refType: schedule.refType === "tag" ? "tag" : "branch",
  branch: schedule.branch || "",
  variables: { ...(schedule.variables || {}) },
  enabled: schedule.enabled !== false,
  skipIfRunning: schedule.skipIfRunning !== false,
});

/** The pipeline a form's choice resolves to — "" is the service default. */
export const pipelineFor = (pipelines, pipelineId) => {
  const list = pipelines || [];
  if (pipelineId) return list.find((item) => String(item.id) === String(pipelineId)) || null;
  return list.find((item) => item.isDefault) || list[0] || null;
};

/**
 * The value each declared input will be built with: what the schedule says,
 * else the input's own default — the same fallback the server applies.
 */
export const effectiveValues = (parameters, variables) =>
  Object.fromEntries(
    (parameters || []).map((param) => {
      const own = (variables || {})[param.name];
      const fallback = param.type === "boolean" ? (String(param.default) === "true" ? "true" : "false") : String(param.default ?? "");
      return [param.name, own === undefined || own === null ? fallback : String(own)];
    })
  );

/**
 * What the API is sent. Only inputs the chosen pipeline declares travel: a
 * value left over from a pipeline the form no longer points at would be
 * refused by the server ("this pipeline has no parameter named …"), and the
 * person never sees that field to fix it.
 */
export const toPayload = (form, parameters) => {
  const declared = new Set((parameters || []).map((param) => param.name));
  const variables = Object.fromEntries(
    Object.entries(form.variables || {}).filter(([name]) => declared.has(name))
  );
  return {
    name: String(form.name || "").trim(),
    cron: String(form.cron || "").trim().replace(/\s+/g, " "),
    timezone: String(form.timezone || "").trim() || "UTC",
    pipelineId: form.pipelineId ? Number(form.pipelineId) : null,
    refType: form.refType === "tag" ? "tag" : "branch",
    branch: String(form.branch || "").trim(),
    variables,
    enabled: Boolean(form.enabled),
    skipIfRunning: Boolean(form.skipIfRunning),
  };
};

/** Local problems worth saying before the round-trip. The server still decides. */
export const formProblems = (form) => {
  const problems = {};
  if (!String(form.name || "").trim()) problems.name = "Give the schedule a name.";
  if (!String(form.cron || "").trim()) problems.cron = "Pick a preset or write a cron expression.";
  if (form.refType === "tag" && !String(form.branch || "").trim()) {
    problems.branch = "Name the tag — a tag has no default to fall back to.";
  }
  return problems;
};

const parseInstant = (iso) => {
  if (!iso) return NaN;
  const raw = String(iso).trim();
  return Date.parse(/(?:[Zz]|[+-]\d{2}:\d{2})$/.test(raw) ? raw : `${raw}Z`);
};

/** "Thu 2 Oct, 02:00" on the wall clock of `timeZone`. */
export const formatRun = (iso, timeZone, { withZone = false } = {}) => {
  const at = parseInstant(iso);
  if (Number.isNaN(at)) return "—";
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: timeZone || "UTC",
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      ...(withZone ? { timeZoneName: "short" } : {}),
    }).format(new Date(at));
  } catch {
    return new Date(at).toISOString().slice(0, 16).replace("T", " ");
  }
};

/** "in 3h", "in 2d", "in 5m" — how far off the next run is. */
export const formatUntil = (iso, now = Date.now()) => {
  const at = parseInstant(iso);
  if (Number.isNaN(at)) return "";
  const seconds = Math.round((at - now) / 1000);
  if (seconds < 60) return "in under a minute";
  if (seconds < 3600) return `in ${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return minutes ? `in ${hours}h ${minutes}m` : `in ${hours}h`;
  }
  return `in ${Math.floor(seconds / 86400)}d`;
};

/**
 * One line on how the last run went, and the tone to draw it in. A skip is
 * not a failure — skip-if-running did its job — but it is not a build either.
 */
export const outcomeOf = (schedule) => {
  if (!schedule.lastOutcome) return { tone: "unknown", label: "Not run yet", detail: "" };
  if (schedule.lastOutcome === "failed") {
    return { tone: "danger", label: "Could not run", detail: schedule.lastError || "" };
  }
  if (schedule.lastOutcome === "skipped") {
    return { tone: "warn", label: "Skipped", detail: schedule.lastError || "" };
  }
  const status = schedule.lastBuild?.status;
  const tone =
    status === "success" ? "ok" : status === "failed" || status === "timeout" ? "danger" : status === "cancelled" ? "warn" : "info";
  return {
    tone,
    label: schedule.lastBuild ? `Build #${schedule.lastBuild.number} ${status || "queued"}` : "Build queued",
    detail: "",
  };
};

/** What the rail says about the section while it is scrolled out of view. */
export const railSummary = (schedules) => {
  if (!schedules) return { text: "", tone: "" };
  if (!schedules.length) return { text: "None", tone: "" };
  const active = schedules.filter((item) => item.enabled).length;
  const broken = schedules.some((item) => item.enabled && (item.lastOutcome === "failed" || item.pipelineProblem));
  return {
    text: active ? `${active} active` : "All paused",
    tone: broken ? "warn" : "",
  };
};

/** Stages that switch on an input, for the line under its control. */
export const conditionsFor = (pipeline, name) =>
  ((pipeline && pipeline.conditions) || []).filter((item) => item.variable === name);

/** "runs only when NIGHTLY_SCAN is true" / "is skipped when … is true" for one condition. */
export const conditionSentence = (condition) =>
  condition.operator === "not_equals"
    ? `${condition.stage} runs unless ${condition.variable} is ${condition.value || "(empty)"}`
    : `${condition.stage} runs only when ${condition.variable} is ${condition.value || "(empty)"}`;
