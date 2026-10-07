/**
 * Pure helpers for the Timetable view — no React, no fetching.
 *
 * The backend computes departures (services/promotion_timetable.py) and the
 * overview says where every application is; these join the two: who is in a
 * departure, what the board says about it, and when an application will reach
 * the top of the ladder.
 */

import { tagOf } from "./promotionModel.js";

/** Board word + colour for each departure status. Colour = meaning, strictly. */
export const BOARD_STATUS = {
  boarding: { text: "BOARDING", tone: "go", label: "Boarding" },
  ondemand: { text: "OPEN", tone: "go", label: "Open · on demand" },
  scheduled: { text: "SCHEDULED", tone: "dim", label: "Scheduled" },
  held: { text: "HELD", tone: "wait", label: "Held" },
  skipped: { text: "SKIPPED", tone: "dim", label: "Skipped" },
  empty: { text: "NO APPS", tone: "dim", label: "Nothing was eligible" },
  approval: { text: "APPROVAL", tone: "wait", label: "Awaiting approval" },
  exception: { text: "EXCEPTION", tone: "wait", label: "Exception · awaiting approval" },
  ready: { text: "APPROVED", tone: "go", label: "Approved · waiting for departure" },
  promoting: { text: "PROMOTING", tone: "go", label: "Promoting" },
  promoted: { text: "PROMOTED", tone: "ok", label: "Promoted" },
  failed: { text: "FAILED", tone: "bad", label: "Failed · rolled back" },
  refused: { text: "REFUSED", tone: "bad", label: "Refused" },
  rejected: { text: "REJECTED", tone: "bad", label: "Rejected by approvers" },
  expired: { text: "EXPIRED", tone: "dim", label: "Expired before approval" },
};

export function boardStatus(dep) {
  if (dep.kind === "ondemand") return BOARD_STATUS.ondemand;
  return BOARD_STATUS[dep.status] || BOARD_STATUS.scheduled;
}

export const NEEDS_ATTENTION = new Set(["held", "approval", "exception", "failed", "refused", "rejected", "expired"]);

/** Labels for an application's place in a departure. */
export const APP_STATE = {
  eligible: { label: "Eligible", tone: "go" },
  soaking: { label: "Soaking", tone: "go-outline" },
  late: { label: "Misses cut-off", tone: "wait" },
  blocked: { label: "Not eligible", tone: "bad" },
  moved: { label: "Moved to next", tone: "dim" },
  sent: { label: "In another release", tone: "dim" },
  promoted: { label: "Promoted", tone: "ok" },
  scheduled: { label: "Deploys at departure", tone: "go" },
  approval: { label: "Awaiting approval", tone: "wait" },
  refused: { label: "Refused", tone: "bad" },
  failed: { label: "Failed", tone: "bad" },
  unchanged: { label: "Already there", tone: "dim" },
};
export const APP_ORDER = ["eligible", "soaking", "scheduled", "approval", "late", "blocked", "refused", "failed", "promoted", "unchanged", "sent", "moved"];

// ── Time ────────────────────────────────────────────────────────────────

export function fmtTime(iso, tz) {
  if (!iso) return "--:--";
  try {
    return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz || undefined });
  } catch {
    return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  }
}

export function dayKey(iso, tz) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString("en-CA", { timeZone: tz || undefined });
  } catch {
    return new Date(iso).toLocaleDateString("en-CA");
  }
}

export function fmtDay(iso, tz) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString("en-GB", { weekday: "short", timeZone: tz || undefined });
  } catch {
    return new Date(iso).toLocaleDateString("en-GB", { weekday: "short" });
  }
}

/** "14:00" today, "Thu 14:00" another day. */
export function fmtWhen(iso, tz, nowIso) {
  if (!iso) return "on demand";
  const same = dayKey(iso, tz) === dayKey(nowIso || new Date().toISOString(), tz);
  return same ? fmtTime(iso, tz) : `${fmtDay(iso, tz)} ${fmtTime(iso, tz)}`;
}

export function minutesUntil(iso, nowMs = Date.now()) {
  if (!iso) return null;
  return Math.round((new Date(iso).getTime() - nowMs) / 60000);
}

// ── Who is in a departure ───────────────────────────────────────────────

/** Index of the hop into ``toEnvironmentId`` (0 = first → second), or -1. */
export function hopIndex(environments, toEnvironmentId) {
  return environments.findIndex((e) => e.id === toEnvironmentId) - 1;
}

/**
 * Every application concerned by a departure, with its state in it.
 * Open departures (boarding, scheduled, on demand, held, skipped) are worked
 * out live from the overview; a closed one reads its release.
 */
export function departureApps(dep, overview, nowMs = Date.now()) {
  if (dep.kind === "release" && dep.release) return releaseApps(dep.release, overview);
  const hop = hopIndex(overview.environments, dep.toEnvironmentId);
  if (hop < 0) return [];
  const excluded = new Set(dep.excluded || []);
  const toCutoff = dep.cutoffAt ? Math.max(0, minutesUntil(dep.cutoffAt, nowMs)) : null;
  const rows = [];
  for (const app of overview.apps) {
    const step = app.steps[hop];
    if (!step) continue;
    let state = null;
    let detail = step.detail;
    switch (step.state) {
      case "ready":
        state = "eligible";
        break;
      case "soaking":
        state = toCutoff == null || step.soakMinutesLeft <= toCutoff ? "soaking" : "late";
        break;
      case "waiting":
        state = "late";
        break;
      case "blocked":
        state = "blocked";
        break;
      case "pending_approval":
        state = "sent";
        break;
      default:
        state = null;
    }
    if (!state) continue;
    if (excluded.has(app.repository)) state = "moved";
    const target = app.cells[hop + 1];
    rows.push({
      key: app.key,
      app,
      state,
      detail,
      fromTag: (target?.tags || []).join(", ") || "—",
      toTag: step.tag || tagOf(step.image),
      image: step.image,
      targets: (step.targets || []).filter((t) => t.image !== step.image),
      soakMinutesLeft: step.soakMinutesLeft,
      passedAt: step.passedAt,
    });
  }
  return rows;
}

const RELEASE_TARGET_STATE = {
  applied: "promoted",
  pending_approval: "approval",
  refused: "refused",
  rejected: "refused",
  failed: "failed",
  expired: "refused",
  unchanged: "unchanged",
};

function releaseApps(release, overview) {
  const byRepo = new Map(overview.apps.map((a) => [a.repository, a]));
  return release.items.map((item) => {
    const states = new Set(item.targets.map((t) => RELEASE_TARGET_STATE[t.status] || "approval"));
    const state = states.has("failed") ? "failed"
      : states.has("refused") && !states.has("promoted") ? "refused"
      : states.has("approval") ? (release.departsAt && new Date(release.departsAt) > new Date() ? "scheduled" : "approval")
      : states.has("promoted") ? "promoted"
      : "unchanged";
    const app = byRepo.get(item.repository) || { key: item.repository, name: item.name, repository: item.repository, system: "—" };
    const refusal = item.targets.find((t) => t.status === "refused");
    return {
      key: item.repository,
      app,
      state,
      detail: refusal?.message || (item.exception ? "Exception — skips the environment below" : ""),
      fromTag: (item.fromTags || []).join(", ") || "—",
      toTag: item.tag,
      image: item.image,
      targets: item.targets,
    };
  });
}

export function countStates(rows) {
  const counts = {};
  for (const row of rows) counts[row.state] = (counts[row.state] || 0) + 1;
  return counts;
}

/** The release a person would promote now: the eligible applications. */
export function promotableItems(rows) {
  return rows
    .filter((r) => r.state === "eligible" && r.targets.length)
    .map((r) => ({
      name: r.app.name,
      image: r.image,
      targets: r.targets.map(({ clusterId, namespace, kind, name, container }) => ({ clusterId, namespace, kind, name, container })),
    }));
}

/** Group rows by system, systems alphabetically, problems first inside. */
export function bySystem(rows) {
  const groups = new Map();
  for (const row of rows) {
    const key = row.app.system || "Ungrouped";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, items]) => ({
      name,
      items: items.sort((a, b) => APP_ORDER.indexOf(a.state) - APP_ORDER.indexOf(b.state) || a.app.name.localeCompare(b.app.name)),
    }));
}

// ── Where's my application ──────────────────────────────────────────────

/**
 * Where an application is and the earliest it can reach each environment
 * above it: the next departure of each hop after it is eligible below.
 */
export function journey(app, overview, timetable, nowMs = Date.now()) {
  const envs = overview.environments;
  const stops = [];
  let clock = nowMs;
  let current = -1;
  app.cells.forEach((cell, i) => {
    if (cell.workloads.length) current = i;
  });
  const newest = app.cells.find((c) => c.images.length)?.images[0];
  for (let i = 0; i < envs.length; i += 1) {
    const cell = app.cells[i];
    const step = i > 0 ? app.steps[i - 1] : null;
    const hasNewest = cell.images.includes(newest);
    if (i === 0 || hasNewest) {
      stops.push({ env: envs[i], state: "done", tag: cell.tags.join(", ") || "—", since: cell.healthySince, cell });
      continue;
    }
    if (!cell.workloads.length) {
      stops.push({ env: envs[i], state: "absent", tag: "—", cell, note: `Not deployed in ${envs[i].name}` });
      continue;
    }
    // The next departure of this hop after the app can be eligible below.
    const soakMs = (envs[i - 1].minSoakMinutes || 0) * 60000;
    const earliest = Math.max(clock, step?.state === "soaking" ? nowMs + (step.soakMinutesLeft || 0) * 60000 : clock + (stops[i - 1]?.state === "next" ? soakMs + 30 * 60000 : 0));
    const dep = nextDeparture(timetable, envs[i].id, earliest);
    stops.push({
      env: envs[i],
      state: stops[i - 1]?.state === "done" ? "next" : "later",
      tag: `${cell.tags.join(", ")} → ${tagOf(newest)}`,
      cell,
      departure: dep,
      step,
    });
    if (dep?.departsAt) clock = new Date(dep.departsAt).getTime() + 30 * 60000;
  }
  const last = stops[stops.length - 1];
  return { stops, current, eta: last.state === "done" ? null : last.departure?.departsAt || null };
}

export function nextDeparture(timetable, toEnvironmentId, afterMs) {
  const deps = timetable.departures.filter(
    (d) => d.toEnvironmentId === toEnvironmentId && ["boarding", "scheduled", "ondemand"].includes(d.kind === "ondemand" ? "ondemand" : d.status),
  );
  const ondemand = deps.find((d) => d.kind === "ondemand");
  if (ondemand) return ondemand;
  return deps
    .filter((d) => d.departsAt && new Date(d.cutoffAt || d.departsAt).getTime() >= afterMs)
    .sort((a, b) => a.departsAt.localeCompare(b.departsAt))[0] || null;
}
