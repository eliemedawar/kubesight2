import { parseApiTime } from "./apiTime.js";

/**
 * Relative time, once.
 *
 * There are five hand-rolled `timeAgo` implementations in this tree
 * (components/clusters, components/mobileApps, components/zoho, lib/integrations,
 * utils/clusterBuilder) and they disagree: on whether "just now" exists, on
 * where minutes become hours, and on whether a naive backend timestamp is
 * treated as UTC. The last one is not cosmetic — reading a naive value as local
 * time shifts every duration by the viewer's UTC offset, which on a freshness
 * indicator means confidently reporting that stale data is current.
 *
 * `parseApiTime` already solves the parsing half. This is the formatting half.
 * New code uses it; the existing copies converge as their pages are touched.
 */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function relativeTime(value, now = Date.now()) {
  const ts = parseApiTime(value);
  if (!Number.isFinite(ts)) {
    return "";
  }

  const delta = now - ts;

  // Small negative deltas are clock skew between browser and server, not the
  // future. Reporting "in 4 seconds" for a row that was just written reads as a
  // bug; reporting "just now" is both truer and less alarming.
  if (delta < -MINUTE) {
    return "in the future";
  }
  if (delta < MINUTE) {
    return "just now";
  }
  if (delta < HOUR) {
    const minutes = Math.floor(delta / MINUTE);
    return `${minutes}m ago`;
  }
  if (delta < DAY) {
    const hours = Math.floor(delta / HOUR);
    return `${hours}h ago`;
  }
  const days = Math.floor(delta / DAY);
  if (days < 30) {
    return `${days}d ago`;
  }
  return new Date(ts).toLocaleDateString();
}

/**
 * How much to trust a timestamp.
 *
 * `staleAfterMs` is the point past which the caller considers data no longer
 * current — a dashboard polling every 30s is stale at a few minutes, a nightly
 * sync is not stale until tomorrow. There is no sensible global default, so it
 * is required rather than guessed.
 */
export function freshness(value, { staleAfterMs, now = Date.now() } = {}) {
  const ts = parseApiTime(value);
  if (!Number.isFinite(ts)) {
    return { state: "unknown", label: "Never", ageMs: null };
  }
  const ageMs = Math.max(0, now - ts);
  const state = staleAfterMs && ageMs > staleAfterMs ? "stale" : "fresh";
  return { state, label: relativeTime(value, now), ageMs };
}
