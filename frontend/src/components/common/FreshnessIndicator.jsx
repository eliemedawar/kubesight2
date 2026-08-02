import { freshness } from "../../lib/relativeTime.js";
import { parseApiTime } from "../../lib/apiTime.js";

/**
 * When this data was last known good.
 *
 * Carries the absolute timestamp in `title` alongside the relative label,
 * because "12m ago" is the right thing to read at a glance and the wrong thing
 * to put in an incident timeline. Both, one element.
 *
 * A null timestamp renders "Never", not an empty space — the absence of a
 * successful fetch is information, and blank looks like a rendering fault.
 */
export default function FreshnessIndicator({
  timestamp,
  staleAfterMs,
  prefix = "Updated",
  className = "",
}) {
  const { state, label } = freshness(timestamp, { staleAfterMs });
  const ts = parseApiTime(timestamp);
  const absolute = Number.isFinite(ts) ? new Date(ts).toLocaleString() : "No successful update recorded";

  return (
    <span
      className={`freshness freshness--${state} ${className}`.trim()}
      title={absolute}
      data-freshness={state}
    >
      {state === "unknown" ? label : `${prefix} ${label}`}
    </span>
  );
}
