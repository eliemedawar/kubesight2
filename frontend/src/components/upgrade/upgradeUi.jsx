/** Shared pieces of the Upgrade Center: status chips, time words, icons.
 *
 *  Colour follows Cardinal: Ember (danger) only for "out of support" and a
 *  blocker, amber for "ending soon" and warnings, teal-blue for "running" or
 *  "waiting on someone", green for done. Every chip carries a glyph and a
 *  word, never colour alone.
 */

import { parseApiTime } from "../../lib/apiTime.js";

export function Chip({ tone = "muted", children, title, className = "" }) {
  return (
    <span className={`uc-chip uc-chip--${tone} ${className}`} title={title}>
      <span className="uc-chip__g" aria-hidden="true" />
      {children}
    </span>
  );
}

export const ms = (value) => {
  const parsed = parseApiTime(value);
  return Number.isFinite(parsed) ? parsed : null;
};

export function timeAgo(value) {
  const at = ms(value);
  if (at === null) return "";
  const seconds = Math.round((Date.now() - at) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export function formatWhen(value, { withYear = false } = {}) {
  const at = ms(value);
  if (at === null) return "";
  return new Date(at).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    ...(withYear ? { year: "numeric" } : {}),
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDay(isoDate) {
  if (!isoDate) return "";
  const date = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return isoDate;
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function formatMonth(isoDate) {
  if (!isoDate) return "";
  const date = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return isoDate;
  return date.toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone: "UTC" });
}

export function duration(fromValue, toValue) {
  const from = ms(fromValue);
  const to = toValue ? ms(toValue) : Date.now();
  if (from === null || to === null) return "";
  const minutes = Math.max(0, Math.round((to - from) / 60000));
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

/** Compact duration for tight columns: "48 s", "4 m 12 s", "1 h 05 m". */
export function shortDuration(fromValue, toValue) {
  const from = ms(fromValue);
  const to = toValue ? ms(toValue) : Date.now();
  if (from === null || to === null) return "";
  const seconds = Math.max(0, Math.round((to - from) / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} m ${String(seconds % 60).padStart(2, "0")} s`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} m`;
}

/** The support clock of a version, as a chip. */
export function SupportChip({ support }) {
  if (!support || support.state === "unknown") return <Chip tone="muted">Support unknown</Chip>;
  const est = support.estimated ? " (est.)" : "";
  if (support.state === "ended") {
    return <Chip tone="danger">Ended {formatDay(support.endOfLife)}</Chip>;
  }
  if (support.state === "ending") {
    const days = support.daysLeft;
    return (
      <Chip tone="warn" title={`End of life ${formatDay(support.endOfLife)}${est}`}>
        {days === 0 ? "Ends today" : `Ends in ${days} day${days === 1 ? "" : "s"}`}
      </Chip>
    );
  }
  return (
    <Chip tone="ok" title={`End of life ${formatDay(support.endOfLife)}${est}`}>
      Until {formatMonth(support.endOfLife)}
    </Chip>
  );
}

export const MODE_LABEL = { automatic: "Automatic", guided: "Guided", manual: "Manual" };
export const MODE_TONE = { automatic: "ok", guided: "info", manual: "muted" };

export function ModeChip({ mode, title }) {
  return (
    <Chip tone={MODE_TONE[mode] || "muted"} title={title}>
      {MODE_LABEL[mode] || "Unknown"}
    </Chip>
  );
}

export const STATUS = {
  awaiting_approval: { tone: "info", label: "Waiting for approval" },
  scheduled: { tone: "info", label: "Scheduled" },
  queued: { tone: "run", label: "Starting" },
  running: { tone: "run", label: "Running" },
  paused: { tone: "warn", label: "Paused" },
  guided: { tone: "run", label: "Guided · in progress" },
  completed: { tone: "ok", label: "Completed" },
  failed: { tone: "danger", label: "Failed" },
  stopped: { tone: "warn", label: "Stopped" },
  rejected: { tone: "danger", label: "Rejected" },
  withdrawn: { tone: "muted", label: "Withdrawn" },
  expired: { tone: "muted", label: "Window missed" },
  manual_required: { tone: "muted", label: "Manual" },
};

export function StatusChip({ status, children }) {
  const entry = STATUS[status] || { tone: "muted", label: status || "Unknown" };
  return <Chip tone={entry.tone}>{children || entry.label}</Chip>;
}

export const CHECK_TONE = { passed: "ok", warning: "warn", failed: "danger" };
export const CHECK_WORD = { passed: "Passed", warning: "Warning", failed: "Blocker" };

export const ACTIVE_STATUSES = ["queued", "running", "paused"];
export const PENDING_STATUSES = ["awaiting_approval", "scheduled"];

export function readinessWords(summary) {
  if (!summary) return "";
  const parts = [];
  if (summary.failed) parts.push(`${summary.failed} blocker${summary.failed === 1 ? "" : "s"}`);
  if (summary.warning) parts.push(`${summary.warning} warning${summary.warning === 1 ? "" : "s"}`);
  if (!parts.length) parts.push(`${summary.passed || 0} passed`);
  return parts.join(" · ");
}

export function readinessTone(summary) {
  if (!summary) return "muted";
  if (summary.failed) return "danger";
  if (summary.warning) return "warn";
  return "ok";
}

const svg = (path) =>
  function Icon(props) {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
        strokeLinejoin="round" aria-hidden="true" {...props}>
        {path}
      </svg>
    );
  };

export const IconRefresh = svg(<><path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" /></>);
export const IconArrow = svg(<><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></>);
export const IconBack = svg(<><path d="M19 12H5" /><path d="m11 18-6-6 6-6" /></>);
export const IconCopy = svg(<><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></>);
export const IconPause = svg(<><path d="M9 5v14" /><path d="M15 5v14" /></>);
export const IconPlay = svg(<path d="M7 4.5v15l12-7.5z" />);
export const IconStop = svg(<rect x="6" y="6" width="12" height="12" rx="1.5" />);
export const IconShield = svg(<path d="M12 3 4.5 6v6c0 4.5 3.2 7.8 7.5 9 4.3-1.2 7.5-4.5 7.5-9V6z" />);
export const IconCheck = svg(<path d="m5 12.5 4.5 4.5L19 7.5" />);
export const IconChevron = svg(<path d="m9 6 6 6-6 6" />);
