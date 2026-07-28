/** Pure logic behind the Cluster Builder page.
 *
 *  Everything here is a function of API payloads and the current clock, so the
 *  three ideas that carry the page — the Blueprint, check grouping, and source
 *  readiness — are testable without rendering anything.
 */

import { parseApiTime } from "../lib/apiTime";

export const PHASE_ORDER = [
  "base_prep", "loadbalancer", "pull_images", "init", "cni",
  "join_cp", "join_workers", "verify", "onboard", "addons",
];

export const PHASE_LABELS = {
  base_prep: "Node preparation",
  loadbalancer: "Load balancer + VIP",
  pull_images: "Image pull",
  init: "kubeadm init",
  cni: "CNI install",
  join_cp: "Join control planes",
  join_workers: "Join workers",
  verify: "Cluster verification",
  onboard: "Register in KubeSight",
  addons: "Install add-ons",
};

/** Short form for the horizontal rail, where a cell is ~80px wide. */
export const PHASE_SHORT = {
  base_prep: "Node prep",
  loadbalancer: "LB + VIP",
  pull_images: "Image pull",
  init: "kubeadm init",
  cni: "CNI",
  join_cp: "Join control planes",
  join_workers: "Join workers",
  verify: "Verify",
  onboard: "Register",
  addons: "Add-ons",
};

export const PHASE_NOTES = {
  base_prep: "containerd + kubeadm on every machine, in parallel",
  loadbalancer: "haproxy + keepalived — VIP live before init",
  pull_images: "fails early if the registry is missing anything",
  init: "through the stable endpoint — it goes into the certs",
  cni: "network plugin, pod CIDR applied",
  join_cp: "one at a time, etcd quorum checked between",
  join_workers: "in parallel",
  verify: "nodes Ready · CoreDNS · etcd · smoke pod",
  onboard: "admin.conf → Clusters page, no manual step",
  addons: "selected add-ons, pinned and verified",
};

export const ROLE_LABELS = {
  control_plane: "Control plane",
  worker: "Worker",
  loadbalancer: "Load balancer",
};

const TIER_ORDER = ["loadbalancer", "control_plane", "worker"];
const TIER_LABELS = {
  loadbalancer: "Load balancers",
  control_plane: "Control planes",
  worker: "Workers",
};

/** Statuses that mean "this machine is part of the cluster now". */
const JOINED_STATUSES = new Set(["joined", "ready"]);

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

export function formatElapsed(ms) {
  if (!(ms > 0)) return "0 s";
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds} s`;
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes} min ${totalSeconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** Zero-padded clock for a live build header: 00:11:47. */
export function formatClock(ms) {
  const total = Math.max(Math.floor((ms || 0) / 1000), 0);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
}

export function timeAgo(value, now = Date.now()) {
  const at = parseApiTime(value);
  if (!Number.isFinite(at)) return "";
  const seconds = Math.round((now - at) / 1000);
  if (seconds < 0) return "just now";
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days} d ago`;
  return `${Math.round(days / 7)} wk ago`;
}

export const STALE_AFTER_HOURS = 24;

/** How much a "this worked once" proof is still worth.
 *
 *  A connection that passed a month ago is not the same as one that passed an
 *  hour ago, and the difference is what turns into a build failing in node
 *  preparation — so the age is a first-class state, not a tooltip.
 */
export function freshness(value, now = Date.now(), staleAfterHours = STALE_AFTER_HOURS) {
  const at = parseApiTime(value);
  if (!Number.isFinite(at)) return { never: true, stale: true, text: "never tested", ageHours: null };
  const ageHours = (now - at) / 3600000;
  return {
    never: false,
    stale: ageHours > staleAfterHours,
    text: `tested ${timeAgo(value, now)}`,
    ageHours,
  };
}

function humanMinutes(ms) {
  const minutes = Math.round(ms / 60000);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** How long the original build took.
 *
 *  Prefers the duration banked at first completion: growing a cluster runs the
 *  phase machine again and rewrites finishedAt, which would otherwise turn
 *  "built in 18 min" into the age of the cluster.
 */
export function buildDuration(build) {
  if (Number.isFinite(build?.buildSeconds) && build.buildSeconds > 0) {
    return humanMinutes(build.buildSeconds * 1000);
  }
  const started = parseApiTime(build?.startedAt);
  const finished = parseApiTime(build?.finishedAt);
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return null;
  const ms = finished - started;
  if (!(ms > 0)) return null;
  return humanMinutes(ms);
}

/** A build that already produced a cluster and is running again is growing it.
 *  No separate status is needed — that combination only happens on growth. */
export function isGrowing(build) {
  return Boolean(build?.status === "building" && build?.resultClusterId);
}

/** The clock a running build should show: its own run, not the cluster's age. */
export function runStartedAt(build) {
  return isGrowing(build) && build?.growthStartedAt
    ? build.growthStartedAt
    : build?.startedAt;
}

// ---------------------------------------------------------------------------
// Phases
// ---------------------------------------------------------------------------

/** Phases this build will actually run.
 *
 *  Steps are created lazily, so the progress denominator has to come from the
 *  build's shape rather than from the rows that happen to exist.
 */
export function expectedPhases(build) {
  return PHASE_ORDER.filter((phase) => {
    if (phase === "loadbalancer") return build?.endpointMode === "managed_haproxy";
    if (phase === "join_cp") return (build?.nodeCounts?.controlPlane || 0) > 1;
    if (phase === "join_workers") return (build?.nodeCounts?.worker || 0) > 0;
    if (phase === "addons") return (build?.addons || []).length > 0;
    return true;
  });
}

export function phaseState(steps = []) {
  if (!steps.length) return "todo";
  if (steps.some((step) => step.status === "failed")) return "fail";
  if (steps.some((step) => step.status === "running")) return "now";
  if (steps.every((step) => step.status === "completed" || step.status === "skipped")) return "done";
  return "todo";
}

/** The whole plan as rail cells, each with its state and its steps. */
export function phaseTimeline(build) {
  const steps = build?.steps || [];
  const planned = expectedPhases(build);
  const seen = new Set([
    ...planned,
    ...PHASE_ORDER.filter((phase) => steps.some((step) => step.phase === phase)),
  ]);
  return PHASE_ORDER.filter((phase) => seen.has(phase)).map((phase) => {
    const phaseSteps = steps.filter((step) => step.phase === phase);
    return { phase, steps: phaseSteps, state: phaseState(phaseSteps) };
  });
}

/** The same rail shape from list-payload data alone.
 *
 *  The builds list carries `currentPhase` but not steps, so the Floor's live
 *  strip derives state from position in the plan: everything before the current
 *  phase has run, the current one is running, the rest is ahead. The backend
 *  picks `currentPhase` as whatever is executing, which is what makes this safe.
 */
export function railFromCurrentPhase(build) {
  const plan = expectedPhases(build);
  const currentIndex = plan.indexOf(build?.currentPhase);
  const failed = build?.status === "failed";
  return plan.map((phase, index) => {
    let state = "todo";
    if (currentIndex >= 0 && index < currentIndex) state = "done";
    else if (currentIndex >= 0 && index === currentIndex) state = failed ? "fail" : "now";
    return { phase, steps: [], state };
  });
}

/** Live progress for the header: which phase, how far through the plan. */
export function buildProgress(build) {
  const timeline = phaseTimeline(build);
  const done = timeline.filter((cell) => cell.state === "done").length;
  const current = timeline.find((cell) => cell.state === "now")
    || [...timeline].reverse().find((cell) => cell.state === "fail")
    || [...timeline].reverse().find((cell) => cell.state === "done");
  const position = current ? timeline.indexOf(current) + 1 : 0;
  return {
    timeline,
    total: timeline.length,
    done,
    percent: timeline.length ? Math.round((done / timeline.length) * 100) : 0,
    current: current || null,
    position,
  };
}

/** Where a failed build stopped, and on which machine. */
export function failurePoint(build) {
  const steps = build?.steps || [];
  const failed = steps.filter((step) => step.status === "failed");
  if (!failed.length) return null;
  const step = failed.reduce((worst, candidate) => {
    const rank = (item) => PHASE_ORDER.indexOf(item.phase);
    return rank(candidate) > rank(worst) ? candidate : worst;
  }, failed[0]);
  const node = (build.nodes || []).find((item) => item.id === step.nodeId) || null;
  const timeline = phaseTimeline(build);
  return {
    step,
    node,
    phase: step.phase,
    phaseLabel: PHASE_LABELS[step.phase] || step.phase,
    position: timeline.findIndex((cell) => cell.phase === step.phase) + 1,
    total: timeline.length,
    completedPhases: timeline.filter((cell) => cell.state === "done").length,
  };
}

// ---------------------------------------------------------------------------
// Preflight — grouped by check, not by machine
// ---------------------------------------------------------------------------

const SEVERITY_RANK = { fail: 0, warn: 1, pass: 2 };

/** Collapse per-node check lists into one row per (check, verdict).
 *
 *  Preflight returns checks nested under each machine, so a kernel module
 *  missing on three machines reads as three findings with the same fix. Every
 *  check carries a stable `id`, which is what makes the regrouping possible.
 */
export function groupChecks(preflightResult) {
  const nodes = preflightResult?.nodes || [];
  const groups = new Map();
  let total = 0;

  nodes.forEach((node) => {
    const name = node.hostname || node.address || `node ${node.nodeId}`;
    (node.checks || []).forEach((check) => {
      total += 1;
      const key = `${check.id}|${check.status}`;
      let group = groups.get(key);
      if (!group) {
        group = {
          key,
          id: check.id,
          label: check.label,
          status: check.status,
          hint: "",
          machines: [],
        };
        groups.set(key, group);
      }
      if (!group.hint && check.hint) group.hint = check.hint;
      group.machines.push({
        nodeId: node.nodeId,
        name,
        role: node.role,
        detail: check.detail || "",
      });
    });
  });

  const ordered = [...groups.values()].sort((a, b) => (
    SEVERITY_RANK[a.status] - SEVERITY_RANK[b.status]
    || b.machines.length - a.machines.length
    || String(a.label).localeCompare(String(b.label))
  ));

  const counts = { pass: 0, warn: 0, fail: 0 };
  ordered.forEach((group) => {
    counts[group.status] = (counts[group.status] || 0) + group.machines.length;
  });
  const machineCounts = {
    pass: nodes.filter((node) => node.status === "pass").length,
    warn: nodes.filter((node) => node.status === "warn").length,
    fail: nodes.filter((node) => node.status === "fail").length,
  };

  const passing = ordered.filter((group) => group.status === "pass");
  const passMachines = [...new Set(passing.flatMap((g) => g.machines.map((m) => m.name)))];

  return {
    // Only what needs attention gets its own row.
    attention: ordered.filter((group) => group.status !== "pass"),
    passing,
    passSummary: {
      checkCount: counts.pass,
      groupCount: passing.length,
      machines: passMachines,
    },
    total,
    counts,
    machineCounts,
    nodeCount: nodes.length,
    // "8 warnings, nothing blocking" — the headline states the decision, not the score.
    verdict: counts.fail ? "fail" : counts.warn ? "warn" : "pass",
  };
}

// ---------------------------------------------------------------------------
// Blueprint — one object across configure → verify → build → done
// ---------------------------------------------------------------------------

function tierTargets({ topologyType, endpointMode }) {
  if (topologyType === "single_cp") {
    return {
      loadbalancer: endpointMode === "managed_haproxy" ? 1 : 0,
      control_plane: 1,
      worker: 0,
    };
  }
  return {
    loadbalancer: endpointMode === "managed_haproxy" ? 2 : 0,
    control_plane: 3,
    worker: 0,
  };
}

/** Flag every HA-tier slot that shares an ESXi host with a sibling. */
function markAntiAffinity(tiers) {
  const conflictHosts = new Set();
  ["loadbalancer", "control_plane"].forEach((role) => {
    const byHost = new Map();
    (tiers[role] || []).forEach((slot) => {
      if (!slot.host) return;
      if (!byHost.has(slot.host)) byHost.set(slot.host, []);
      byHost.get(slot.host).push(slot);
    });
    byHost.forEach((slots, host) => {
      if (slots.length < 2) return;
      conflictHosts.add(host);
      slots.forEach((slot) => { slot.tie = true; });
    });
  });
  return [...conflictHosts];
}

function assemble({ tiers, targets, bus, state }) {
  const conflictHosts = markAntiAffinity(tiers);
  return {
    state,
    bus,
    conflictHosts,
    tiers: TIER_ORDER
      .filter((role) => targets[role] > 0 || (tiers[role] || []).length > 0 || role === "control_plane")
      .map((role) => ({
        role,
        label: TIER_LABELS[role],
        target: targets[role] || 0,
        slots: tiers[role] || [],
        filled: (tiers[role] || []).filter((slot) => slot.state !== "empty").length,
      })),
  };
}

function busFor({ endpointMode, vipAddress, controlPlaneEndpoint }, state) {
  if (endpointMode === "managed_haproxy") {
    return {
      managed: true,
      address: vipAddress || "",
      port: "6443",
      label: "VIP",
      state,
    };
  }
  return {
    managed: false,
    address: controlPlaneEndpoint || "",
    port: "",
    label: "Endpoint",
    state,
  };
}

/** Trailing empty slots, so 2 of 3 control planes reads as an unfinished
    drawing rather than as a number. Workers keep one open slot as an invitation. */
function padWithEmpties(tiers, targets) {
  TIER_ORDER.forEach((role) => {
    const need = targets[role];
    const blanks = need
      ? Math.max(need - tiers[role].length, 0)
      : role === "worker" ? 1 : 0;
    for (let index = 0; index < blanks; index += 1) {
      tiers[role].push({
        key: `${role}-empty-${index}`,
        name: "",
        host: null,
        sub: "",
        state: "empty",
        stamp: null,
        tie: false,
      });
    }
  });
}

/**
 * Blueprint for a build being configured in the wizard.
 * @param picked      {[moid]: {role, address, hostname}}
 * @param manualNodes [{role, hostname, address}]
 * @param vms         vCenter inventory, for the ESXi host of each pick
 */
export function draftBlueprint({ basics, picked = {}, manualNodes = [], vms = [] }) {
  const targets = tierTargets(basics || {});
  const vmByMoid = new Map(vms.map((vm) => [vm.moid, vm]));
  const tiers = { loadbalancer: [], control_plane: [], worker: [] };

  Object.entries(picked).forEach(([moid, pick]) => {
    if (!tiers[pick.role]) return;
    const vm = vmByMoid.get(moid);
    tiers[pick.role].push({
      key: `vm-${moid}`,
      name: vm?.name || pick.hostname || moid,
      host: vm?.esxiHost || null,
      sub: vm?.esxiHost || "",
      state: "set",
      stamp: null,
      tie: false,
    });
  });

  manualNodes.forEach((node, index) => {
    if (!tiers[node.role] || !(node.address || node.hostname)) return;
    tiers[node.role].push({
      key: `manual-${index}`,
      name: node.hostname || node.address,
      host: null,
      sub: "manual host",
      state: "set",
      stamp: null,
      tie: false,
    });
  });

  padWithEmpties(tiers, targets);
  return assemble({
    tiers,
    targets,
    bus: busFor(basics || {}, "idle"),
    state: "outline",
  });
}

/** Every ESXi host keyed by the address preflight will report, so the stamped
    drawing keeps the placement it had while machines were being picked. */
export function hostByAddress({ picked = {}, vms = [] }) {
  const vmByMoid = new Map(vms.map((vm) => [vm.moid, vm]));
  const map = {};
  Object.entries(picked).forEach(([moid, pick]) => {
    const vm = vmByMoid.get(moid);
    const address = pick.address || vm?.guestIp;
    if (address && vm?.esxiHost) map[address] = vm.esxiHost;
  });
  return map;
}

/** The stamped blueprint: built from the preflight result, because those nodes
    are exactly what will be built. Keyed by node, so no name matching. */
export function preflightBlueprint(basics, preflightResult, hosts = {}) {
  const targets = tierTargets(basics || {});
  const tiers = { loadbalancer: [], control_plane: [], worker: [] };
  (preflightResult?.nodes || []).forEach((node) => {
    if (!tiers[node.role]) return;
    const host = hosts[node.address] || null;
    tiers[node.role].push({
      key: `pf-${node.nodeId}`,
      nodeId: node.nodeId,
      name: node.hostname || node.address,
      host,
      sub: host || node.address,
      state: "set",
      stamp: node.status === "fail" ? "bad" : node.status === "warn" ? "warn" : "ok",
      tie: false,
    });
  });
  padWithEmpties(tiers, targets);
  return assemble({
    tiers,
    targets,
    bus: busFor(basics || {}, "checked"),
    state: "stamped",
  });
}

/** Blueprint for a persisted build — the same drawing, now reporting reality. */
export function buildBlueprint(build) {
  const targets = tierTargets(build || {});
  const steps = build?.steps || [];
  const runningNodeIds = new Set(
    steps.filter((step) => step.status === "running" && step.nodeId).map((step) => step.nodeId)
  );
  const completed = build?.status === "completed";
  // Before a build runs, "assigned" is the right reading for every machine.
  // Once it runs, an untouched machine is waiting, not merely assigned.
  const started = ["building", "failed", "cancelled", "completed"].includes(build?.status);
  const lbUp = steps.some(
    (step) => step.phase === "loadbalancer" && step.status === "completed"
  ) || completed;

  const tiers = { loadbalancer: [], control_plane: [], worker: [] };
  (build?.nodes || []).forEach((node) => {
    if (!tiers[node.role]) return;
    const joined = completed || JOINED_STATUSES.has(node.status);
    const failed = node.status === "failed";
    const live = !joined && !failed && runningNodeIds.has(node.id);
    // Once a build has begun, a machine nobody has touched must not wear its
    // role colour — a red control-plane box reads as a problem, and the only
    // thing separating it from a failed one would be a 17px badge.
    const untouched = started && node.status === "pending";
    const state = failed
      ? "failed"
      : joined ? "joined" : live ? "live" : untouched ? "waiting" : "set";
    tiers[node.role].push({
      key: `node-${node.id}`,
      nodeId: node.id,
      name: node.hostname || node.vsphereVmName || node.address,
      host: node.vsphereHost || null,
      sub: node.isPrimaryCp
        ? "primary"
        : node.isLbMaster
          ? "VRRP master"
          : live
            ? "working…"
            : node.vsphereHost || (node.addressSource === "manual" ? "manual host" : ""),
      state,
      stamp: failed ? "bad" : joined ? "ok" : live ? "live" : node.preflight?.status === "warn" ? "warn" : null,
      tie: false,
    });
  });

  const state = completed
    ? "built"
    : build?.status === "building"
      ? "live"
      : (build?.status === "failed" || build?.status === "cancelled")
        ? "stopped"
        : "stamped";
  return assemble({
    tiers,
    targets,
    bus: busFor(build || {}, lbUp ? "up" : "idle"),
    state,
  });
}

// ---------------------------------------------------------------------------
// Sources readiness
// ---------------------------------------------------------------------------

const SUDO_LABELS = {
  nopasswd: "passwordless sudo",
  password: "sudo with password",
  root: "root login",
};
const POLICY_LABELS = {
  pinned: "host keys pinned",
  strict: "strict host keys",
  tofu: "trust on first use",
};

/** One line describing what an SSH route actually is. */
export function sshPosture(profile, credentials = []) {
  const credential = credentials.find((item) => item.id === profile?.credentialId);
  return [
    credential?.authMethod === "password" ? "password auth" : credential ? "key auth" : "",
    SUDO_LABELS[credential?.sudoMode] || "",
    POLICY_LABELS[profile?.hostKeyPolicy] || "",
  ].filter(Boolean).join(" · ");
}

/** The three questions a source profile answers, as answers. */
export function sourceProfileSummary(profile) {
  if (!profile) {
    return {
      packages: "Upstream internet repositories",
      packagesDetail: "dev and test only",
      images: "Their upstream registries",
      imagesDetail: "",
      obstacles: ["No outbound proxy"],
      incomplete: "",
    };
  }
  const mode = profile.repoMode || "internet";
  const packages = mode === "mirror"
    ? "Internal package mirror"
    : mode === "offline"
      ? "Offline bundle"
      : "Upstream internet repositories";
  const packagesDetail = mode === "mirror"
    ? profile.k8sPkgRepoUrl || ""
    : mode === "offline"
      ? profile.offlineBundlePath || ""
      : "";
  const proxied = Boolean(profile.k8sImageRegistry);
  const obstacles = [];
  if (profile.httpProxy || profile.httpsProxy) {
    obstacles.push(`Outbound proxy ${profile.httpsProxy || profile.httpProxy}`);
  } else {
    obstacles.push("No outbound proxy");
  }
  if (profile.extraCaConfigured) obstacles.push("1 trusted CA certificate");
  if (profile.noProxy) obstacles.push(`NO_PROXY ${profile.noProxy}`);

  let incomplete = "";
  if (mode === "mirror" && !profile.k8sPkgRepoUrl) incomplete = "Mirror URL is missing.";
  if (mode === "offline" && !profile.offlineBundlePath) incomplete = "Bundle path is missing.";

  return {
    packages,
    packagesDetail,
    images: proxied ? "A registry proxy" : "Their upstream registries",
    imagesDetail: proxied ? profile.k8sImageRegistry : "",
    obstacles,
    incomplete,
  };
}

/** What a new build should start with, given what is already healthy.
 *
 *  "Healthy" has to mean freshest-proof-wins, not first-in-the-list: the API
 *  orders these by name, so picking the first match hands the wizard whichever
 *  record happens to sort earliest — a route last proved twelve days ago, or a
 *  source profile that cannot work as configured.
 */
export function preferredSources(infra = {}, now = Date.now()) {
  const byFreshest = (rows, field) => [...rows]
    .map((row) => ({ row, age: freshness(row[field], now).ageHours ?? Infinity }))
    .sort((a, b) => a.age - b.age)[0]?.row;

  const vsphere = infra.vsphere || [];
  const profiles = infra.profiles || [];
  const buildProfiles = infra.buildProfiles || [];

  const vcenter = byFreshest(
    vsphere.filter((row) => row.lastConnectionStatus === "ok"), "lastTestedAt"
  ) || vsphere[0] || null;
  const route = byFreshest(
    profiles.filter((row) => row.lastTestStatus === "ok"), "lastTestAt"
  ) || profiles[0] || null;
  // A profile that is missing its mirror URL or bundle path would fail the
  // build, so it is never the default even when it sorts first.
  const usable = buildProfiles.filter((row) => !sourceProfileSummary(row).incomplete);
  return { vcenter, route, buildProfile: usable[0] || null };
}

/** How many catalog add-ons can install with no internet at all. */
export function bundleCoverage(addonCatalog = []) {
  const total = addonCatalog.length;
  const bundled = addonCatalog.filter((addon) => (addon.bundledVersions || []).length > 0).length;
  return { total, bundled, complete: total > 0 && bundled === total };
}

const SEGMENT_RANK = { bad: 0, warn: 1, idle: 2, live: 3, ok: 4 };

/** "Can I start a build right now?" — the question the page opens with.
 *
 *  Counting connections never answered it. Each segment reports a state and,
 *  where a proof exists, how old that proof is.
 */
export function deriveReadiness({
  builds = [],
  infra = {},
  addonCatalog = [],
  canManageInfra = false,
  now = Date.now(),
}) {
  const vsphere = infra.vsphere || [];
  const profiles = infra.profiles || [];
  const credentials = infra.credentials || [];
  const buildProfiles = infra.buildProfiles || [];
  const segments = [];

  if (canManageInfra) {
    const ok = vsphere.filter((row) => row.lastConnectionStatus === "ok");
    const failed = vsphere.filter(
      (row) => row.lastConnectionStatus && row.lastConnectionStatus !== "ok"
    );
    const newest = ok
      .map((row) => ({ row, fresh: freshness(row.lastTestedAt, now) }))
      .sort((a, b) => (a.fresh.ageHours ?? Infinity) - (b.fresh.ageHours ?? Infinity))[0];
    if (!vsphere.length) {
      segments.push({
        key: "vsphere", label: "vCenter", state: "idle",
        value: "None configured", sub: "the machine picker needs one", fix: "Add a vCenter",
      });
    } else if (newest && !newest.fresh.stale) {
      segments.push({
        key: "vsphere", label: "vCenter", state: "ok",
        value: newest.row.name || newest.row.baseUrl, sub: newest.fresh.text, fix: "",
      });
    } else if (newest) {
      segments.push({
        key: "vsphere", label: "vCenter", state: "warn",
        value: newest.row.name || newest.row.baseUrl, sub: "", fix: `Last reached ${timeAgo(newest.row.lastTestedAt, now)} — re-test`,
      });
    } else {
      segments.push({
        key: "vsphere", label: "vCenter", state: failed.length ? "bad" : "warn",
        value: `${vsphere.length} connection${vsphere.length === 1 ? "" : "s"}`,
        sub: "", fix: failed.length ? "Last test failed — re-test" : "Never tested — run a test",
      });
    }

    const tested = profiles
      .filter((row) => row.lastTestStatus === "ok")
      .map((row) => ({ row, fresh: freshness(row.lastTestAt, now) }))
      .sort((a, b) => (a.fresh.ageHours ?? Infinity) - (b.fresh.ageHours ?? Infinity))[0];
    const anyFailed = profiles.some((row) => row.lastTestStatus === "failed");
    if (!profiles.length) {
      segments.push({
        key: "ssh", label: "SSH reach", state: "idle",
        value: "No routes", sub: "KubeSight needs a way in", fix: "Add a route",
      });
    } else if (tested && !tested.fresh.stale) {
      segments.push({
        key: "ssh", label: "SSH reach", state: "ok",
        value: tested.row.name,
        sub: sshPosture(tested.row, credentials) || tested.fresh.text,
        fix: "",
      });
    } else {
      const primary = tested?.row || profiles[0];
      segments.push({
        key: "ssh", label: "SSH reach", state: anyFailed ? "bad" : "warn",
        value: primary?.name || `${profiles.length} routes`,
        sub: sshPosture(primary, credentials),
        fix: anyFailed
          ? "Last test failed — re-test"
          : tested ? `Last proved ${timeAgo(tested.row.lastTestAt, now)} — re-test` : "Never tested — test against a node",
      });
    }
  }

  const coverage = bundleCoverage(addonCatalog);
  const broken = buildProfiles
    .map((profile) => ({ profile, summary: sourceProfileSummary(profile) }))
    .find((entry) => entry.summary.incomplete);
  const bundleLine = coverage.total
    ? `add-on bundles ${coverage.bundled} of ${coverage.total} present`
    : "";
  if (broken) {
    segments.push({
      key: "sources", label: "Packages & images", state: "warn",
      value: broken.profile.name, sub: bundleLine, fix: broken.summary.incomplete,
    });
  } else if (!buildProfiles.length) {
    segments.push({
      key: "sources", label: "Packages & images", state: "idle",
      value: "Internet defaults",
      sub: bundleLine || "upstream packages and registries",
      fix: coverage.complete ? "" : "",
    });
  } else {
    const primary = buildProfiles[0];
    segments.push({
      key: "sources", label: "Packages & images", state: coverage.complete ? "ok" : "warn",
      value: primary.name,
      sub: sourceProfileSummary(primary).packages,
      fix: coverage.complete ? "" : `${bundleLine} — offline builds need the rest`,
    });
  }

  const inFlight = builds.filter(
    (build) => build.status === "building" || build.status === "preflighting"
  );
  const born = builds.filter((build) => build.status === "completed" && build.resultClusterId);
  segments.push({
    key: "builds", label: "Builds", state: inFlight.length ? "live" : "ok",
    value: inFlight.length
      ? `${inFlight.length} in flight`
      : `${born.length} cluster${born.length === 1 ? "" : "s"} built`,
    sub: inFlight.length ? inFlight[0].name : born[0] ? `last: ${born[0].name}` : "none yet",
    fix: "",
  });

  const worst = segments.reduce(
    (lowest, segment) => (SEGMENT_RANK[segment.state] < SEGMENT_RANK[lowest] ? segment.state : lowest),
    "ok"
  );
  const needsAttention = segments.filter(
    (segment) => segment.state === "warn" || segment.state === "bad"
  );
  const blocking = segments.filter(
    (segment) => segment.state === "idle" && segment.key !== "sources"
  );

  return {
    segments,
    state: blocking.length ? "blocked" : needsAttention.length ? "attention" : "ready",
    worst,
    headline: blocking.length
      ? "Not ready to build"
      : needsAttention.length ? "Ready to build" : "Ready to build",
    sub: blocking.length
      ? `${blocking.map((segment) => segment.label.toLowerCase()).join(" and ")} not configured`
      : needsAttention.length
        ? `${needsAttention.length} source${needsAttention.length === 1 ? "" : "s"} need${needsAttention.length === 1 ? "s" : ""} a re-test`
        : "every source is fresh",
    // Proportion for the verdict ring: green up to the first thing that is not ok.
    okCount: segments.filter((segment) => segment.state === "ok" || segment.state === "live").length,
    total: segments.length,
  };
}

// ---------------------------------------------------------------------------
// Builds list
// ---------------------------------------------------------------------------

const IN_FLIGHT_STATUSES = new Set(["building", "preflighting"]);
/** Statuses that are waiting on a person: a failure to look at, a draft to
    finish, a preflight that passed and never got launched. */
const ATTENTION_STATUSES = new Set([
  "failed", "cancelled", "preflight_failed", "draft", "preflight_passed",
]);

/** Split the library by what each build wants from you, not by date. */
export function groupBuilds(builds = []) {
  return {
    inFlight: builds.filter((build) => IN_FLIGHT_STATUSES.has(build.status)),
    attention: builds.filter(
      (build) => !IN_FLIGHT_STATUSES.has(build.status) && ATTENTION_STATUSES.has(build.status)
    ),
    done: builds.filter(
      (build) => !IN_FLIGHT_STATUSES.has(build.status) && !ATTENTION_STATUSES.has(build.status)
    ),
  };
}

// ---------------------------------------------------------------------------
// Add-ons
// ---------------------------------------------------------------------------

export function addonDisplayName(addon, catalog = []) {
  const id = typeof addon === "string" ? addon : addon?.id;
  const match = catalog.find((item) => item.id === id);
  if (match?.displayName) return match.displayName;
  return String(id || "Unknown add-on")
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function addonSummary(addons = [], catalog = [], includeVersions = false) {
  if (!addons.length) return "None";
  return addons.map((addon) => {
    const name = addonDisplayName(addon, catalog);
    const version = typeof addon === "object" ? addon.version : "";
    return includeVersions && version ? `${name} v${version}` : name;
  }).join(", ");
}

/** sha256:9f4a…c21e — enough to recognize, short enough to sit in a card. */
export function shortDigest(digest) {
  const raw = String(digest || "").trim();
  if (raw.length < 12) return raw;
  return `sha256:${raw.slice(0, 4)}…${raw.slice(-4)}`;
}

/** Where this add-on's manifest comes from at the selected version. */
export function addonProvenance(catalogEntry, version) {
  const digests = catalogEntry?.manifestDigestsByVersion?.[version]
    || catalogEntry?.manifestDigests
    || [];
  const bundled = (catalogEntry?.bundledVersions || []).includes(version);
  return {
    bundled,
    digest: digests[0]?.sha256 ? shortDigest(digests[0].sha256) : "",
    manifestCount: digests.length,
    text: bundled ? "bundled" : "downloads on build",
  };
}

// ---------------------------------------------------------------------------
// Kubernetes-version compatibility
//
// CNI plugins and add-ons support a moving window of Kubernetes minors, so the
// catalog ships the whole matrix and the wizard narrows it to whatever the user
// picked. Offering an incompatible pairing and failing at preflight would be a
// worse experience than never offering it.
// ---------------------------------------------------------------------------

/** "1.32.4" / "v1.32.4" → "1.32"; anything unparseable → "". */
export function k8sMinorOf(version) {
  const match = /^v?(\d+)\.(\d+)(?:\.\d+)?$/.exec(String(version || "").trim());
  return match ? `${match[1]}.${match[2]}` : "";
}

/** Catalog-entry versions validated on this Kubernetes version, newest first. */
export function versionsForK8s(catalogEntry, k8sVersion) {
  const versions = catalogEntry?.versions || [];
  const minor = k8sMinorOf(k8sVersion);
  const matrix = catalogEntry?.k8sMinorsByVersion;
  // No matrix (older backend) means no basis to filter — show everything
  // rather than silently emptying the picker.
  if (!minor || !matrix) return [...versions];
  return versions.filter((version) => (matrix[version] || []).includes(minor));
}

export function defaultVersionForK8s(catalogEntry, k8sVersion) {
  return versionsForK8s(catalogEntry, k8sVersion)[0] || "";
}

/** CNI plugins with at least one version usable on this Kubernetes version. */
export function cniPluginsForK8s(catalog = [], k8sVersion) {
  return catalog.filter((plugin) => versionsForK8s(plugin, k8sVersion).length > 0);
}
