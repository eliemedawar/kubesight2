/**
 * Date ranges for the Audit Logs page.
 *
 * The API takes `from` (inclusive) and `to` (exclusive) as ISO instants and
 * reads them as UTC. Quick ranges count back from the moment they are picked,
 * so paging further back keeps the same window. A custom range is picked as
 * local calendar days: from the start of the first day to the start of the
 * day after the last, in the viewer's own time zone.
 */

export const QUICK_RANGES = [
  { key: "all", label: "Any time", ms: null },
  { key: "24h", label: "24 h", ms: 24 * 3600 * 1000 },
  { key: "7d", label: "7 days", ms: 7 * 24 * 3600 * 1000 },
  { key: "30d", label: "30 days", ms: 30 * 24 * 3600 * 1000 },
  { key: "custom", label: "Custom", ms: null },
];

/** Local midnight of a "YYYY-MM-DD" day, or null when it is not one. */
function localDayStart(day) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ""));
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "YYYY-MM-DD" of a Date, in local time (what <input type="date"> holds). */
export function localDay(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/**
 * The { from, to } ISO bounds for a range, or { error } when a custom range
 * cannot be used. Empty sides are left out (open-ended).
 */
export function rangeBounds(key, { fromDay = "", toDay = "" } = {}, now = Date.now()) {
  const quick = QUICK_RANGES.find((range) => range.key === key);
  if (quick?.ms) return { from: new Date(now - quick.ms).toISOString() };
  if (key !== "custom") return {};
  const start = fromDay ? localDayStart(fromDay) : null;
  const lastDay = toDay ? localDayStart(toDay) : null;
  if ((fromDay && !start) || (toDay && !lastDay)) return { error: "Pick a valid date." };
  const end = lastDay
    ? new Date(lastDay.getFullYear(), lastDay.getMonth(), lastDay.getDate() + 1)
    : null;
  if (start && end && end <= start) return { error: "The end date is before the start date." };
  const out = {};
  if (start) out.from = start.toISOString();
  if (end) out.to = end.toISOString();
  return out;
}

/** One line saying what the range covers, for the results summary. */
export function describeRange(key, { fromDay = "", toDay = "" } = {}) {
  const quick = QUICK_RANGES.find((range) => range.key === key);
  if (quick?.ms) return `in the last ${quick.label}`;
  if (key !== "custom" || (!fromDay && !toDay)) return "";
  if (fromDay && toDay) return fromDay === toDay ? `on ${fromDay}` : `from ${fromDay} to ${toDay}`;
  return fromDay ? `since ${fromDay}` : `up to ${toDay}`;
}
