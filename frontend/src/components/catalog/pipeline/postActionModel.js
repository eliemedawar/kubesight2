/**
 * Post actions — what a pipeline does when a build ends (Jenkins' `post {}`).
 *
 * Pure helpers for the editor: the kinds, their `when` choices, a blank of
 * each, the problems Save would hit (the server checks the same rules —
 * services/ci/post_actions.py — this only says so before the round trip),
 * and the comparison that tells the save bar something changed.
 *
 * The two halves run at different moments, and the copy built from these
 * helpers says so:
 *   - notifications (email, webhook) go out once the BUILD is over — after any
 *     approval or deploy stage — for the result their `when` names;
 *   - cleanup commands run in the build workspace when the last stage on the
 *     runner ends, with the stages' outcome, and never change the build's
 *     result.
 */

export const MAX_POST_ACTIONS = 10;
export const MAX_RECIPIENTS = 25;
export const DEFAULT_CLEANUP_TIMEOUT = 600;
export const MIN_CLEANUP_TIMEOUT = 30;
export const MAX_CLEANUP_TIMEOUT = 1800;

export const POST_ACTION_TYPES = [
  {
    value: "email",
    label: "Email",
    icon: "message",
    verb: "Send an email",
    description: "Tell people how the build went: result, branch, failed stage, tests and a link.",
  },
  {
    value: "webhook",
    label: "Webhook",
    icon: "link",
    verb: "Post to a webhook",
    description: "Slack, Microsoft Teams or any URL that takes JSON. The URL is kept as a secret.",
  },
  {
    value: "commands",
    label: "Cleanup",
    icon: "terminal",
    verb: "Run cleanup commands",
    description: "Shell commands in the build workspace once the stages are done — release a lock, delete a temp namespace.",
  },
];

export const postActionType = (value) => POST_ACTION_TYPES.find((type) => type.value === value);

const WHEN = {
  always: { value: "always", label: "Always", short: "always" },
  success: { value: "success", label: "On success", short: "on success" },
  failure: { value: "failure", label: "On failure", short: "on failure" },
  fixed: { value: "fixed", label: "When fixed", short: "when fixed" },
};

/** The `when` choices a kind offers. A cleanup runs with the outcome of THIS
 * build's stages, which knows nothing of the previous build — so no "fixed". */
export function whenOptions(type) {
  const base = [WHEN.always, WHEN.success, WHEN.failure];
  if (type === "commands") {
    return base.map((option) => ({
      ...option,
      hint:
        option.value === "always"
          ? "Whatever the stages did"
          : option.value === "success"
            ? "Every stage passed"
            : "A stage failed",
    }));
  }
  return [
    { ...WHEN.always, hint: "Every finished build, cancelled ones too" },
    { ...WHEN.success, hint: "The build succeeded" },
    { ...WHEN.failure, hint: "Failed or timed out" },
    { ...WHEN.fixed, hint: "First success after a failure" },
  ];
}

export const whenLabel = (when) => (WHEN[when] || WHEN.always).short;

export const WEBHOOK_FORMATS = [
  { value: "slack", label: "Slack", hint: "Incoming webhook" },
  { value: "teams", label: "Teams", hint: "Workflow webhook" },
  { value: "json", label: "JSON", hint: "Any endpoint" },
];

export const webhookFormatLabel = (format) =>
  ({ slack: "Slack", teams: "Teams", json: "Webhook" })[format] || "Webhook";

let keySeed = 0;
/** A client-only identity, so React keys and selection survive edits. */
export const withPostKey = (action) => ({ ...action, _key: `pa${(keySeed += 1)}` });

export function blankPostAction(type) {
  if (type === "email") {
    return { type, when: "failure", recipients: [], subject: "", message: "" };
  }
  if (type === "webhook") {
    return { type, when: "failure", format: "slack", urlSecret: "" };
  }
  return {
    type: "commands",
    when: "always",
    name: "Cleanup",
    commands: [],
    image: "",
    workingDirectory: "",
    env: {},
    secretRefs: [],
    timeoutSeconds: DEFAULT_CLEANUP_TIMEOUT,
  };
}

/** Switching kind keeps what both kinds share (when), drops the rest. A
 * "fixed" notification turned into a cleanup becomes "always". */
export function changePostType(action, type) {
  const next = blankPostAction(type);
  const when = type === "commands" && action.when === "fixed" ? "always" : action.when || next.when;
  return { ...next, when, _key: action._key };
}

/** The title a row carries — matches the server's label for the build's row. */
export function postActionTitle(action) {
  if (action.type === "webhook") return webhookFormatLabel(action.format);
  if (action.type === "commands") return String(action.name || "").trim() || "Cleanup";
  return postActionType(action.type)?.label || "Post action";
}

const lines = (commands) => (commands || []).map((line) => String(line ?? "")).filter((line) => line.trim());

/** One line saying what the action does. */
export function postActionSummary(action) {
  if (action.type === "email") {
    const recipients = action.recipients || [];
    if (!recipients.length) return "No recipients yet";
    return recipients.length === 1 ? recipients[0] : `${recipients[0]} and ${recipients.length - 1} more`;
  }
  if (action.type === "webhook") {
    return action.urlSecret ? `URL from secret ${action.urlSecret}` : "No URL secret chosen";
  }
  const commands = lines(action.commands);
  if (!commands.length) return "No commands yet";
  return commands.length === 1 ? commands[0].trim() : `${commands[0].trim()} · +${commands.length - 1} more`;
}

const EMAIL_RE = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/;

/** Addresses as the server stores them: split on commas, spaces and
 * semicolons, trimmed, without repeats (case-insensitively). */
export function parseRecipients(text) {
  const seen = [];
  for (const raw of String(text || "").split(/[\s,;]+/)) {
    const address = raw.trim();
    if (address && !seen.some((item) => item.toLowerCase() === address.toLowerCase())) seen.push(address);
  }
  return seen;
}

/**
 * What Save would refuse, per field: `[{ field, message }]`. The server is the
 * authority; this is the same rules, said while the person is still typing.
 */
export function postActionProblems(action, index, list = [], secretKeys = []) {
  const problems = [];
  const known = new Set((secretKeys || []).map((item) => (typeof item === "string" ? item : item.key)));
  if (!postActionType(action.type)) {
    problems.push({ field: "type", message: "Choose what this post action does." });
    return problems;
  }
  if (action.type === "commands" && action.when === "fixed") {
    problems.push({ field: "when", message: "Cleanup runs with this build's outcome, so it cannot be “when fixed”." });
  }
  if (action.type === "email") {
    const recipients = action.recipients || [];
    if (!recipients.length) problems.push({ field: "recipients", message: "Add at least one recipient." });
    const bad = recipients.find((address) => address.length > 254 || !EMAIL_RE.test(address));
    if (bad) problems.push({ field: "recipients", message: `“${bad}” is not an email address.` });
    if (recipients.length > MAX_RECIPIENTS) {
      problems.push({ field: "recipients", message: `At most ${MAX_RECIPIENTS} recipients.` });
    }
  }
  if (action.type === "webhook") {
    if (!action.urlSecret) {
      problems.push({ field: "urlSecret", message: "Choose the secret that holds the webhook URL." });
    } else if (known.size && !known.has(action.urlSecret)) {
      problems.push({ field: "urlSecret", message: `Secret “${action.urlSecret}” no longer exists.` });
    }
  }
  if (action.type === "commands") {
    if (!lines(action.commands).length) problems.push({ field: "commands", message: "Add the commands to run." });
    const seconds = Number(action.timeoutSeconds);
    if (action.timeoutSeconds !== undefined && action.timeoutSeconds !== "" &&
        (!Number.isFinite(seconds) || seconds < MIN_CLEANUP_TIMEOUT || seconds > MAX_CLEANUP_TIMEOUT)) {
      problems.push({ field: "timeoutSeconds", message: "The time limit must be between 30 seconds and 30 minutes." });
    }
    const name = postActionTitle(action).toLowerCase();
    const clash = list.some(
      (other, position) => position !== index && other.type === "commands" && postActionTitle(other).toLowerCase() === name
    );
    if (clash) problems.push({ field: "name", message: "Another cleanup has this name." });
    const missing = (action.secretRefs || []).find((ref) => known.size && !known.has(ref.name));
    if (missing) problems.push({ field: "secretRefs", message: `Secret “${missing.name}” no longer exists.` });
  }
  return problems;
}

/** The list as the API stores it: no editor identity, only the kind's fields. */
export function postActionsForApi(list) {
  return (list || []).map((action) => {
    if (action.type === "email") {
      return {
        type: "email",
        when: action.when || "always",
        recipients: [...(action.recipients || [])],
        subject: action.subject || "",
        message: action.message || "",
      };
    }
    if (action.type === "webhook") {
      return { type: "webhook", when: action.when || "always", format: action.format || "json", urlSecret: action.urlSecret || "" };
    }
    return {
      type: "commands",
      when: action.when || "always",
      name: action.name || "Cleanup",
      commands: [...(action.commands || [])],
      image: action.image || null,
      workingDirectory: action.workingDirectory || null,
      env: { ...(action.env || {}) },
      secretRefs: (action.secretRefs || []).map((ref) => ({ name: ref.name, envVar: ref.envVar || ref.name })),
      timeoutSeconds: Number(action.timeoutSeconds) || DEFAULT_CLEANUP_TIMEOUT,
    };
  });
}

// The server drops leading/trailing blank lines and trailing spaces, so a list
// that only differs by those is not a change.
const trimLines = (commands) => {
  const out = (commands || []).map((line) => String(line ?? "").replace(/\s+$/, ""));
  while (out.length && !out[0].trim()) out.shift();
  while (out.length && !out[out.length - 1].trim()) out.pop();
  return out;
};

const comparable = (list) =>
  JSON.stringify(
    postActionsForApi(list).map((action) =>
      action.type === "commands" ? { ...action, commands: trimLines(action.commands) } : action
    )
  );

/** Whether the post actions differ from the saved ones. */
export const postActionsChanged = (saved, current) => comparable(saved) !== comparable(current);

/** "new" / "edited" / null for one action, against the saved list. */
export function postActionChange(action, saved = []) {
  if (!action._key) return null;
  const before = saved.find((item) => item._key === action._key);
  if (!before) return "new";
  return comparable([before]) === comparable([action]) ? null : "edited";
}

/** The sentence the flow block opens with. */
export function postActionsSentence(list) {
  const notify = (list || []).filter((action) => action.type !== "commands").length;
  const cleanup = (list || []).length - notify;
  const parts = [];
  if (notify) parts.push(`${notify} ${notify === 1 ? "notification" : "notifications"}`);
  if (cleanup) parts.push(`${cleanup} ${cleanup === 1 ? "cleanup" : "cleanups"}`);
  return parts.length ? parts.join(" · ") : "Nothing happens yet";
}

export const moveAction = (list, from, to) => {
  if (to < 0 || to >= list.length || from === to) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
};

/** For the build drawer: what one post-action row on a build says. */
export function postRunVerdict(item) {
  if (!item) return "";
  const notify = item.type === "email" || item.type === "webhook";
  if (item.status === "success") return item.detail || (notify ? "Sent." : "Ran and exited 0.");
  if (item.status === "skipped") return item.detail || (notify ? "Not sent." : "Did not run.");
  if (item.status === "pending") {
    if (notify && item.phase === "queued") return item.attempts ? "Retrying shortly." : "Queued to send.";
    return notify ? "Waits for the build to end." : "Waits for the stages to end.";
  }
  if (item.status === "running") return notify ? "Sending…" : "Running…";
  if (item.status === "timeout") return item.error || "Timed out.";
  if (item.status === "cancelled") return item.error || "Cancelled.";
  return item.error || (notify ? "Not delivered." : "Failed.");
}
