/**
 * Pure helpers for the Promotions page — no React, no fetching.
 *
 * The backend decides (services/promotion_service.py, promotion_overview.py);
 * these only shape its overview payload for the views: a gate's queue, the
 * filters and grouping a 100+ application estate needs, the versions a person
 * may pick, and the plan of a release before it is sent.
 */

export const MODES = [
  { key: "off", label: "Off", hint: "No rule. Anything may be deployed here." },
  {
    key: "warn",
    label: "Warn",
    hint: "Deploys go ahead; one that skipped the environment before is recorded.",
  },
  {
    key: "enforce",
    label: "Enforce",
    hint: "Only images that passed the environment before. Skipping needs an approved exception.",
  },
];

export const SOAK_PRESETS = [
  { minutes: 0, label: "None" },
  { minutes: 30, label: "30 min" },
  { minutes: 60, label: "1 h" },
  { minutes: 240, label: "4 h" },
  { minutes: 1440, label: "1 day" },
  { minutes: 4320, label: "3 days" },
];

/** A hop's states, in the order a gate's queue shows them. */
export const GATE_STATES = [
  { key: "ready", label: "Ready", tone: "accent", hint: "Passed the environment below — promote them." },
  { key: "soaking", label: "Soaking", tone: "info", hint: "Healthy below, but the soak time has not passed." },
  { key: "waiting", label: "Not healthy yet", tone: "muted", hint: "Still rolling out below." },
  { key: "pending_approval", label: "Awaiting approval", tone: "info", hint: "Already sent — waiting in a change bundle." },
  { key: "blocked", label: "Blocked", tone: "warn", hint: "A mutable tag such as latest cannot be promoted." },
  { key: "in_sync", label: "In sync", tone: "ok", hint: "Both environments run the same version." },
  { key: "not_deployed", label: "Not deployed above", tone: "muted", hint: "No workload in the target environment yet." },
];

export const STATE_META = Object.fromEntries(GATE_STATES.map((s) => [s.key, s]));

export const APP_FILTERS = [
  { key: "all", label: "All" },
  { key: "behind", label: "Behind" },
  { key: "drift", label: "Skipped a step" },
  { key: "rolling", label: "Rolling out" },
  { key: "approval", label: "Awaiting approval" },
  { key: "gaps", label: "Missing somewhere" },
];

// ── Formatting ──────────────────────────────────────────────────────────

/** "1 workload", "3 workloads". */
export function plural(count, word, many = `${word}s`) {
  return `${count} ${count === 1 ? word : many}`;
}

export function formatMinutes(minutes) {
  const value = Number(minutes) || 0;
  if (value <= 0) return "none";
  if (value < 60) return `${value} min`;
  if (value % 1440 === 0) return `${value / 1440} day${value === 1440 ? "" : "s"}`;
  const hours = Math.floor(value / 60);
  const rest = value % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

/** "3 d", "5 h", "12 min" — how long since `iso`. */
export function formatAge(iso, now = Date.now()) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const minutes = Math.max(0, Math.round((now - then) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} d`;
}

export function formatRelative(iso, now = Date.now()) {
  const age = formatAge(iso, now);
  if (!age) return "";
  return age === "now" ? "just now" : `${age} ago`;
}

export function tagOf(image) {
  const text = String(image || "");
  const last = text.split("/").pop() || "";
  if (last.includes("@")) return last.split("@")[1].slice(0, 19);
  return last.includes(":") ? last.split(":").pop() : text;
}

export function defaultReleaseName(envName, date = new Date()) {
  const day = date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${envName} · ${day} ${time}`;
}

// ── The ladder ──────────────────────────────────────────────────────────

export function envIndex(environments, envId) {
  return (environments || []).findIndex((env) => env.id === envId);
}

export function gateKey(gate) {
  return gate ? `${gate.fromEnvironmentId}-${gate.toEnvironmentId}` : "";
}

/** The gate with the most applications ready — where "Promote…" lands. */
export function busiestGate(gates) {
  let best = null;
  for (const gate of gates || []) {
    if (!best || gate.counts.ready > best.counts.ready) best = gate;
  }
  return best;
}

// ── Filtering + grouping ────────────────────────────────────────────────

export function matchesSearch(app, term) {
  const t = (term || "").trim().toLowerCase();
  if (!t) return true;
  return (
    app.name.toLowerCase().includes(t) ||
    app.repository.toLowerCase().includes(t) ||
    (app.system || "").toLowerCase().includes(t) ||
    (app.team || "").toLowerCase().includes(t) ||
    (app.namespaces || []).some((ns) => ns.includes(t))
  );
}

export function filterApps(apps, { search = "", systems = [], filter = "all" } = {}) {
  const systemSet = new Set(systems);
  return (apps || []).filter((app) => {
    if (systemSet.size && !systemSet.has(app.system)) return false;
    if (!matchesSearch(app, search)) return false;
    switch (filter) {
      case "behind":
        return app.lag > 0;
      case "drift":
        return app.drift;
      case "rolling":
        return app.cells.some((c) => c.state === "progressing");
      case "approval":
        return app.steps.some((s) => s.state === "pending_approval");
      case "gaps": {
        const first = app.cells.findIndex((c) => c.workloads.length);
        return first >= 0 && app.cells.slice(first).some((c) => !c.workloads.length);
      }
      default:
        return true;
    }
  });
}

/** [{ key, label, items }] in label order; `by` is "system" | "team" | "none". */
export function groupApps(items, by = "system") {
  if (by === "none") return [{ key: "all", label: "", items }];
  const groups = new Map();
  for (const item of items) {
    const app = item.app || item;
    const label = (by === "team" ? app.team : app.system) || (by === "team" ? "No team" : "Ungrouped");
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(item);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([label, groupItems]) => ({ key: label, label, items: groupItems }));
}

export function systemsOf(apps) {
  const counts = new Map();
  for (const app of apps || []) counts.set(app.system, (counts.get(app.system) || 0) + 1);
  return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, count]) => ({ name, count }));
}

// ── A gate's queue ──────────────────────────────────────────────────────

/** One row per application for the hop at `gateIdx` (0 = first → second). */
export function gateQueue(apps, gateIdx) {
  return (apps || []).map((app) => ({
    app,
    step: app.steps[gateIdx],
    source: app.cells[gateIdx],
    target: app.cells[gateIdx + 1],
  }));
}

export function countByState(rows) {
  const counts = {};
  for (const row of rows) counts[row.step.state] = (counts[row.step.state] || 0) + 1;
  return counts;
}

const SORTS = {
  name: (a, b) => a.app.name.localeCompare(b.app.name),
  waiting: (a, b) => (a.step.passedAt || "9").localeCompare(b.step.passedAt || "9"),
  system: (a, b) => a.app.system.localeCompare(b.app.system) || a.app.name.localeCompare(b.app.name),
};

export function sortRows(rows, by = "name") {
  return [...rows].sort(SORTS[by] || SORTS.name);
}

// ── Choosing a version ──────────────────────────────────────────────────

/**
 * Images a person may pick when promoting an app into the environment at
 * `targetIdx`: what runs in every environment below it, nearest first.
 * `distance` 1 = the environment right below (a normal promotion); more = it
 * skips environments and needs an exception.
 */
export function versionChoices(app, environments, targetIdx) {
  if (targetIdx <= 0 || !app) return [];
  const seen = new Set();
  const choices = [];
  for (let index = targetIdx - 1; index >= 0; index -= 1) {
    const env = environments[index];
    const cell = app.cells[index];
    for (const image of cell?.images || []) {
      if (seen.has(image)) continue;
      seen.add(image);
      const workloads = cell.workloads.filter((w) => w.image === image);
      choices.push({
        image,
        tag: tagOf(image),
        environmentId: env.id,
        environmentName: env.name,
        distance: targetIdx - index,
        healthy: workloads.some((w) => w.state === "healthy"),
        skips: environments.slice(index + 1, targetIdx).map((e) => e.name),
      });
    }
  }
  return choices;
}

// ── Planning a release ──────────────────────────────────────────────────

/**
 * What a release would do. `picks` is Map(appKey → { image, exception }),
 * `rows` the gate rows by app key. Returns the items to send plus the facts
 * the review screen states: workloads, clusters, which need approval.
 */
export function planRelease(picks, rowsByKey, clusters) {
  const items = [];
  const clusterIds = new Set();
  let workloads = 0;
  let exceptions = 0;
  for (const [key, pick] of picks) {
    const row = rowsByKey.get(key);
    if (!row) continue;
    const targets = (row.target?.workloads || [])
      .filter((w) => w.image !== pick.image)
      .map((w) => ({
        clusterId: w.clusterId,
        namespace: w.namespace,
        kind: w.kind,
        name: w.name,
        container: w.container,
        fromTag: w.tag,
      }));
    if (!targets.length) continue;
    targets.forEach((t) => clusterIds.add(t.clusterId));
    workloads += targets.length;
    if (pick.exception) exceptions += 1;
    items.push({
      key,
      app: row.app,
      image: pick.image,
      tag: tagOf(pick.image),
      fromTags: [...new Set(targets.map((t) => t.fromTag))],
      exception: Boolean(pick.exception),
      skips: pick.skips || [],
      passedAt: pick.exception ? null : row.step.passedAt,
      targets,
    });
  }
  const gated = [...clusterIds].filter((id) => (clusters?.[id]?.requiredApprovals || 0) > 0);
  return {
    items,
    workloads,
    exceptions,
    clusters: [...clusterIds],
    gatedClusters: gated,
    needsApproval: gated.length > 0,
  };
}

/** The release body the API takes. */
export function releasePayload(plan, { environmentId, name, reference, note, exceptionReason }) {
  return {
    environmentId,
    name,
    reference,
    note,
    exceptionReason: plan.exceptions ? exceptionReason : "",
    items: plan.items.map((item) => ({
      name: item.app.name,
      image: item.image,
      targets: item.targets.map(({ fromTag: _fromTag, ...t }) => t),
    })),
  };
}

// ── Releases + activity ─────────────────────────────────────────────────

export const RELEASE_STATUS = {
  applied: { label: "Deployed", tone: "ok" },
  pending_approval: { label: "Awaiting approval", tone: "info" },
  partial: { label: "Partly deployed", tone: "warn" },
  refused: { label: "Refused", tone: "danger" },
  rejected: { label: "Rejected", tone: "danger" },
  failed: { label: "Failed", tone: "danger" },
  expired: { label: "Expired", tone: "muted" },
  unchanged: { label: "Unchanged", tone: "muted" },
  empty: { label: "Nothing to do", tone: "muted" },
};

export function groupByDay(entries, now = new Date()) {
  const groups = [];
  const today = now.toDateString();
  const yesterday = new Date(now.getTime() - 86400000).toDateString();
  for (const entry of entries || []) {
    const date = new Date(entry.createdAt);
    const key = Number.isNaN(date.getTime()) ? "Unknown" : date.toDateString();
    const label =
      key === today
        ? "Today"
        : key === yesterday
          ? "Yesterday"
          : date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
    let group = groups[groups.length - 1];
    if (!group || group.key !== key) {
      group = { key, label, items: [] };
      groups.push(group);
    }
    group.items.push(entry);
  }
  return groups;
}
