/**
 * Pure helpers for the redesigned Alerts feed: severity ranking, firing
 * durations, policy grouping, and 24h history bucketing for the activity
 * strip / sparklines / resolved stats. No DOM, fully unit-testable.
 */

const HOUR_MS = 3600000;
const DAY_MS = 24 * HOUR_MS;

/** Display-only severity mapping: critical → danger, warning → warn, everything else → info. */
export function severityInfo(severity) {
  const value = String(severity || "info").toLowerCase();
  if (value === "critical") {
    return { rank: 0, tone: "danger", label: value };
  }
  if (value === "warning") {
    return { rank: 1, tone: "warn", label: value };
  }
  return { rank: 2, tone: "info", label: value };
}

/**
 * Backend timestamps are naive-UTC isoformat (no timezone suffix); parse them
 * as UTC, not local time, or firing durations drift by the UTC offset.
 */
export function parseAlertTime(value) {
  if (!value) {
    return NaN;
  }
  const raw = String(value);
  const normalized = /[Zz]|[+-]\d{2}:\d{2}$/.test(raw) ? raw : `${raw}Z`;
  return Date.parse(normalized);
}

/** "42 min" / "1 h 03" / "2 d 4 h" — compact duration for feed rows and TTR cells. */
export function formatDurationShort(ms) {
  if (!Number.isFinite(ms) || ms < 0) {
    return "";
  }
  const totalMinutes = Math.floor(ms / 60000);
  if (totalMinutes < 1) {
    return "under 1 min";
  }
  if (totalMinutes < 60) {
    return `${totalMinutes} min`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours < 24) {
    return minutes ? `${hours} h ${String(minutes).padStart(2, "0")}` : `${hours} h`;
  }
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours ? `${days} d ${remHours} h` : `${days} d`;
}

export function formatFiringDuration(firedAt, nowTs = Date.now()) {
  const ts = parseAlertTime(firedAt);
  if (Number.isNaN(ts)) {
    return "";
  }
  return formatDurationShort(nowTs - ts);
}

function groupKeyFor(alert) {
  if (alert?.policyId != null && alert.policyId !== "") {
    return `p:${alert.policyId}`;
  }
  if (alert?.policyName) {
    return `n:${alert.policyName}`;
  }
  return null;
}

/**
 * Collapse alerts sharing a policy into groups (≥2 members); everything else
 * stays a flat single. Entries are sorted worst-severity first, then
 * oldest-fired first — the alert that has waited longest outranks the newest.
 *
 * Returns entries shaped as either:
 *   { kind: "group", key, title, alertType, worst, count, oldestTs, alerts }
 *   { kind: "single", key, alert, severity, firedTs }
 */
export function groupAlerts(alerts = []) {
  const decorated = alerts.map((alert, index) => ({
    alert,
    index,
    severity: severityInfo(alert.severity),
    firedTs: (() => {
      const ts = parseAlertTime(alert.firedAt);
      return Number.isNaN(ts) ? Number.MAX_SAFE_INTEGER : ts;
    })(),
    groupKey: groupKeyFor(alert),
  }));

  const byKey = new Map();
  for (const item of decorated) {
    if (!item.groupKey) {
      continue;
    }
    if (!byKey.has(item.groupKey)) {
      byKey.set(item.groupKey, []);
    }
    byKey.get(item.groupKey).push(item);
  }

  const entries = [];
  const consumed = new Set();
  for (const [key, members] of byKey.entries()) {
    if (members.length < 2) {
      continue;
    }
    members.sort((a, b) => a.firedTs - b.firedTs);
    members.forEach((m) => consumed.add(m.index));
    const worst = members.reduce(
      (acc, m) => (m.severity.rank < acc.rank ? m.severity : acc),
      members[0].severity
    );
    entries.push({
      kind: "group",
      key,
      title: members[0].alert.policyName || members[0].alert.title || "Alert policy",
      alertType: members[0].alert.alertType || "metric",
      worst,
      count: members.length,
      oldestTs: members[0].firedTs,
      alerts: members.map((m) => m.alert),
    });
  }

  for (const item of decorated) {
    if (consumed.has(item.index)) {
      continue;
    }
    entries.push({
      kind: "single",
      key: item.groupKey ? `${item.groupKey}:${item.alert.id}` : `solo:${item.alert.id ?? item.index}`,
      alert: item.alert,
      severity: item.severity,
      firedTs: item.firedTs,
    });
  }

  entries.sort((a, b) => {
    const rankA = a.kind === "group" ? a.worst.rank : a.severity.rank;
    const rankB = b.kind === "group" ? b.worst.rank : b.severity.rank;
    if (rankA !== rankB) {
      return rankA - rankB;
    }
    const tsA = a.kind === "group" ? a.oldestTs : a.firedTs;
    const tsB = b.kind === "group" ? b.oldestTs : b.firedTs;
    return tsA - tsB;
  });

  return entries;
}

function severityKey(severity) {
  const value = String(severity || "").toLowerCase();
  if (value === "critical" || value === "warning") {
    return value;
  }
  return "info";
}

/**
 * Bucket history rows by fired hour over the trailing window for the activity
 * strip. Buckets are aligned to local clock hours, oldest-first; the last
 * bucket is the current (partial) hour.
 */
export function bucketAlertHistory(items = [], { nowTs = Date.now(), hours = 24 } = {}) {
  const currentHour = new Date(nowTs);
  currentHour.setMinutes(0, 0, 0);
  const currentHourStart = currentHour.getTime();
  const windowStart = currentHourStart - (hours - 1) * HOUR_MS;

  const buckets = Array.from({ length: hours }, (_, i) => ({
    startTs: windowStart + i * HOUR_MS,
    critical: 0,
    warning: 0,
    info: 0,
    total: 0,
  }));
  let total = 0;
  for (const item of items) {
    const ts = parseAlertTime(item.firedAt);
    if (Number.isNaN(ts) || ts > nowTs || ts < windowStart) {
      continue;
    }
    const index = Math.min(hours - 1, Math.floor((ts - windowStart) / HOUR_MS));
    const bucket = buckets[index];
    bucket[severityKey(item.severity)] += 1;
    bucket.total += 1;
    total += 1;
  }
  const maxTotal = buckets.reduce((max, b) => Math.max(max, b.total), 0);
  return { buckets, maxTotal, total };
}

export function severitySeries(buckets = [], key) {
  return buckets.map((bucket) => bucket[key] || 0);
}

/**
 * Resolved-alert stats over the trailing window: count, median time-to-resolve
 * and the most recently resolved alert (for the all-clear state).
 */
export function resolvedStats(items = [], { nowTs = Date.now(), windowMs = DAY_MS } = {}) {
  const resolved = [];
  for (const item of items) {
    if (String(item.status || "").toLowerCase() !== "resolved") {
      continue;
    }
    const resolvedTs = parseAlertTime(item.resolvedAt);
    if (Number.isNaN(resolvedTs) || nowTs - resolvedTs >= windowMs || resolvedTs > nowTs) {
      continue;
    }
    const firedTs = parseAlertTime(item.firedAt);
    const durationMs =
      !Number.isNaN(firedTs) && resolvedTs >= firedTs ? resolvedTs - firedTs : null;
    resolved.push({ item, resolvedTs, durationMs });
  }

  const durations = resolved
    .map((entry) => entry.durationMs)
    .filter((value) => value != null)
    .sort((a, b) => a - b);
  let medianMs = null;
  if (durations.length) {
    const mid = Math.floor(durations.length / 2);
    medianMs =
      durations.length % 2 ? durations[mid] : (durations[mid - 1] + durations[mid]) / 2;
  }

  let lastResolved = null;
  for (const entry of resolved) {
    if (!lastResolved || entry.resolvedTs > lastResolved.resolvedTs) {
      lastResolved = entry;
    }
  }

  return {
    count: resolved.length,
    medianMs,
    lastResolved: lastResolved
      ? {
          title: lastResolved.item.policyName || lastResolved.item.title || "Alert",
          resourceName: lastResolved.item.resourceName || lastResolved.item.pod || "",
          resolvedTs: lastResolved.resolvedTs,
          durationMs: lastResolved.durationMs,
        }
      : null,
  };
}
