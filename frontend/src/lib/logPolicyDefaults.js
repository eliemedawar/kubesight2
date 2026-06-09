import { ALL_RESOURCES_VALUE } from "../components/alerts/PolicyScopeFields.jsx";

export const LOG_DEFAULT_PATTERN = "ERROR, Exception, failed, timeout";

export const AUTO_RECEIVER_GROUP_NAMES = ["devops", "devops team", "operations"];

export const LOG_TOUCH_KEYS = {
  scope: "scope",
  logConfig: "logConfig",
  logPattern: "logPattern",
  logMatchType: "logMatchType",
  logCaseSensitive: "logCaseSensitive",
  logWindow: "logWindow",
  logContext: "logContext",
  receivers: "receivers",
  evaluationInterval: "evaluationInterval",
};

const ALLOWED_LOG_WINDOW_SECONDS = [60, 300, 900, 1800];

export function logWindowSecondsForEvaluationInterval(evalSeconds) {
  const seconds = Number(evalSeconds) || 300;
  if (ALLOWED_LOG_WINDOW_SECONDS.includes(seconds)) {
    return seconds;
  }
  const ceiling = ALLOWED_LOG_WINDOW_SECONDS.find((value) => value >= seconds);
  return ceiling ?? ALLOWED_LOG_WINDOW_SECONDS[ALLOWED_LOG_WINDOW_SECONDS.length - 1];
}

export function resolveDefaultNamespace(activeNamespace, namespaceOptions = []) {
  if (activeNamespace) {
    return activeNamespace;
  }
  const first = namespaceOptions[0];
  if (!first) {
    return "";
  }
  return typeof first === "string" ? first : first.name || "";
}

export function findAutoReceiverGroupId(receiverGroups = []) {
  const targets = new Set(AUTO_RECEIVER_GROUP_NAMES);
  const match = receiverGroups.find((group) =>
    targets.has(String(group.name || "").trim().toLowerCase())
  );
  return match?.id ?? null;
}

export function buildDefaultLogConfig(evaluationIntervalSeconds) {
  return {
    matchType: "contains",
    pattern: LOG_DEFAULT_PATTERN,
    caseSensitive: false,
    logWindowSeconds: logWindowSecondsForEvaluationInterval(evaluationIntervalSeconds),
    contextLinesBefore: 5,
    contextLinesAfter: 5,
    maxLines: 20,
  };
}

/**
 * Apply log alert defaults for new policies, skipping fields the user already edited.
 */
export function applyLogPolicyDefaults(form, context, touched) {
  const touchSet = touched instanceof Set ? touched : new Set();
  const evaluationIntervalSeconds =
    form.evaluationIntervalSeconds ?? context.defaultEvaluationIntervalSeconds ?? 300;
  const next = { ...form, alertType: "log" };

  if (!touchSet.has(LOG_TOUCH_KEYS.scope)) {
    next.scope = {
      type: "deployment",
      namespace: resolveDefaultNamespace(context.activeNamespace, context.namespaceOptions),
      resourceName: ALL_RESOURCES_VALUE,
    };
  }

  const currentLogConfig = form.logConfig || {};
  const logConfig = { ...currentLogConfig };

  if (!touchSet.has(LOG_TOUCH_KEYS.logConfig) && !touchSet.has(LOG_TOUCH_KEYS.logMatchType)) {
    logConfig.matchType = "contains";
  }
  if (!touchSet.has(LOG_TOUCH_KEYS.logConfig) && !touchSet.has(LOG_TOUCH_KEYS.logPattern)) {
    logConfig.pattern = LOG_DEFAULT_PATTERN;
  }
  if (!touchSet.has(LOG_TOUCH_KEYS.logConfig) && !touchSet.has(LOG_TOUCH_KEYS.logCaseSensitive)) {
    logConfig.caseSensitive = false;
  }
  if (!touchSet.has(LOG_TOUCH_KEYS.logConfig) && !touchSet.has(LOG_TOUCH_KEYS.logWindow)) {
    logConfig.logWindowSeconds = logWindowSecondsForEvaluationInterval(evaluationIntervalSeconds);
  }
  if (!touchSet.has(LOG_TOUCH_KEYS.logConfig) && !touchSet.has(LOG_TOUCH_KEYS.logContext)) {
    logConfig.contextLinesBefore = 5;
    logConfig.contextLinesAfter = 5;
    logConfig.maxLines = 20;
  }

  next.logConfig = logConfig;

  if (!touchSet.has(LOG_TOUCH_KEYS.receivers)) {
    const groupId = findAutoReceiverGroupId(context.receiverGroups);
    next.receiverGroupIds = groupId ? [groupId] : [];
    next.receiverIds = [];
  }

  return next;
}

export function shouldShowLogReceiverWarning(form) {
  if (form.alertType !== "log") {
    return false;
  }
  const receiverIds = form.receiverIds || [];
  const receiverGroupIds = form.receiverGroupIds || [];
  return receiverIds.length === 0 && receiverGroupIds.length === 0;
}
