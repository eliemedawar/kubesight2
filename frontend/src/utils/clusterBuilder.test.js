import { describe, expect, it } from "vitest";
import {
  addonProvenance,
  buildBlueprint,
  buildDuration,
  bundleCoverage,
  cniPluginsForK8s,
  defaultVersionForK8s,
  k8sMinorOf,
  versionsForK8s,
  deriveReadiness,
  draftBlueprint,
  expectedPhases,
  failurePoint,
  formatClock,
  freshness,
  groupBuilds,
  groupChecks,
  hostByAddress,
  isGrowing,
  phaseTimeline,
  preferredSources,
  preflightBlueprint,
  railFromCurrentPhase,
  runStartedAt,
  shortDigest,
  sourceProfileSummary,
  sshPosture,
  timeAgo,
  imageStatusText,
  isWholeNamespaceSelected,
  removeWorkload,
  selectedInNamespace,
  toggleWholeNamespace,
  toggleWorkload,
  expandNamespaceSelection,
  workloadPlanVerdict,
  workloadReceipt,
  workloadSelectionSummary,
  emptyStorage,
  setAllStorageDecisions,
  setStorageDecision,
  storageDecision,
  storageErrors,
  storageNeeds,
  storageRows,
  storageSummary,
} from "./clusterBuilder.js";

const NOW = Date.parse("2026-07-27T12:00:00Z");
const isoAgo = (hours) => new Date(NOW - hours * 3600000).toISOString();

const HA_BASICS = {
  topologyType: "stacked_ha",
  endpointMode: "managed_haproxy",
  vipAddress: "10.42.10.101",
};

describe("freshness", () => {
  it("treats an untested record as stale rather than green", () => {
    expect(freshness(null, NOW)).toMatchObject({ never: true, stale: true, text: "never tested" });
  });

  it("keeps a recent proof fresh and ages an old one out", () => {
    expect(freshness(isoAgo(2), NOW)).toMatchObject({ never: false, stale: false });
    expect(freshness(isoAgo(2), NOW).text).toBe("tested 2 h ago");
    expect(freshness(isoAgo(9 * 24), NOW).stale).toBe(true);
  });

  it("honours a caller-supplied staleness window", () => {
    expect(freshness(isoAgo(30), NOW, 48).stale).toBe(false);
  });
});

describe("timeAgo", () => {
  it("scales the unit with the age", () => {
    expect(timeAgo(isoAgo(0.2), NOW)).toBe("12 min ago");
    expect(timeAgo(isoAgo(5), NOW)).toBe("5 h ago");
    expect(timeAgo(isoAgo(72), NOW)).toBe("3 d ago");
    expect(timeAgo(isoAgo(24 * 21), NOW)).toBe("3 wk ago");
  });

  it("parses naive backend timestamps as UTC", () => {
    // isoformat() can omit the suffix; a local reading would drift by the offset.
    expect(timeAgo("2026-07-27T10:00:00", NOW)).toBe("2 h ago");
  });
});

describe("formatClock", () => {
  it("pads to a stable width so the digits do not jump", () => {
    expect(formatClock(0)).toBe("00:00:00");
    expect(formatClock(707000)).toBe("00:11:47");
    expect(formatClock(3723000)).toBe("01:02:03");
  });
});

describe("groupChecks", () => {
  const result = {
    status: "warn",
    nodes: [
      {
        nodeId: 1, hostname: "cp-01", address: "10.0.0.21", role: "control_plane", status: "warn",
        checks: [
          { id: "ssh", label: "SSH connectivity", status: "pass", detail: "", hint: "" },
          { id: "swap", label: "Swap disabled", status: "warn", detail: "2 GiB swap on", hint: "Run swapoff -a." },
        ],
      },
      {
        nodeId: 2, hostname: "cp-02", address: "10.0.0.22", role: "control_plane", status: "warn",
        checks: [
          { id: "ssh", label: "SSH connectivity", status: "pass", detail: "", hint: "" },
          { id: "swap", label: "Swap disabled", status: "warn", detail: "4 GiB swap on", hint: "" },
          { id: "br_netfilter", label: "br_netfilter loaded", status: "warn", detail: "", hint: "Loaded during prep." },
        ],
      },
      {
        nodeId: 3, hostname: "w-01", address: "10.0.0.31", role: "worker", status: "pass",
        checks: [{ id: "ssh", label: "SSH connectivity", status: "pass", detail: "", hint: "" }],
      },
    ],
  };

  it("reports one row per check instead of repeating it per machine", () => {
    const { attention } = groupChecks(result);
    const swap = attention.find((group) => group.id === "swap");
    expect(swap.machines.map((m) => m.name)).toEqual(["cp-01", "cp-02"]);
    expect(attention.filter((group) => group.id === "swap")).toHaveLength(1);
  });

  it("orders by severity then by how many machines are affected", () => {
    const { attention } = groupChecks({
      nodes: [
        { nodeId: 1, hostname: "a", role: "worker", status: "fail", checks: [
          { id: "ports", label: "Ports free", status: "fail" },
          { id: "swap", label: "Swap disabled", status: "warn" },
        ] },
        { nodeId: 2, hostname: "b", role: "worker", status: "warn", checks: [
          { id: "swap", label: "Swap disabled", status: "warn" },
        ] },
      ],
    });
    expect(attention.map((group) => group.id)).toEqual(["ports", "swap"]);
  });

  it("carries the first hint on record so the fix is stated once", () => {
    const swap = groupChecks(result).attention.find((group) => group.id === "swap");
    expect(swap.hint).toBe("Run swapoff -a.");
  });

  it("collapses everything green into a summary", () => {
    const { passing, passSummary, attention } = groupChecks(result);
    expect(attention.every((group) => group.status !== "pass")).toBe(true);
    expect(passing).toHaveLength(1);
    expect(passSummary.checkCount).toBe(3);
    expect(passSummary.machines).toEqual(["cp-01", "cp-02", "w-01"]);
  });

  it("states the decision, not just the score", () => {
    expect(groupChecks(result).verdict).toBe("warn");
    expect(groupChecks(result).counts).toEqual({ pass: 3, warn: 3, fail: 0 });
    expect(groupChecks(result).machineCounts).toEqual({ pass: 1, warn: 2, fail: 0 });
  });

  it("survives an empty result", () => {
    expect(groupChecks(null)).toMatchObject({ total: 0, verdict: "pass", nodeCount: 0 });
  });
});

describe("draftBlueprint", () => {
  const vms = [
    { moid: "vm-1", name: "lb-01", esxiHost: "esx-04", guestIp: "10.42.10.11" },
    { moid: "vm-2", name: "cp-01", esxiHost: "esx-04", guestIp: "10.42.10.21" },
    { moid: "vm-3", name: "cp-02", esxiHost: "esx-05", guestIp: "10.42.10.22" },
    { moid: "vm-4", name: "cp-03", esxiHost: "esx-05", guestIp: "10.42.10.23" },
  ];

  it("draws every tier the topology needs, with empty slots for the rest", () => {
    const plan = draftBlueprint({ basics: HA_BASICS, vms });
    expect(plan.tiers.map((tier) => tier.role)).toEqual([
      "loadbalancer", "control_plane", "worker",
    ]);
    expect(plan.tiers[0].slots).toHaveLength(2);
    expect(plan.tiers[1].slots).toHaveLength(3);
    expect(plan.tiers[0].slots.every((slot) => slot.state === "empty")).toBe(true);
    expect(plan.tiers[1].filled).toBe(0);
  });

  it("omits the load-balancer tier when KubeSight does not manage the endpoint", () => {
    const plan = draftBlueprint({
      basics: { ...HA_BASICS, endpointMode: "external_lb", controlPlaneEndpoint: "api.example:6443" },
    });
    expect(plan.tiers.map((tier) => tier.role)).toEqual(["control_plane", "worker"]);
    expect(plan.bus).toMatchObject({ managed: false, address: "api.example:6443" });
  });

  it("fills slots as machines are assigned", () => {
    const plan = draftBlueprint({
      basics: HA_BASICS,
      picked: { "vm-2": { role: "control_plane" }, "vm-3": { role: "control_plane" } },
      vms,
    });
    const cps = plan.tiers.find((tier) => tier.role === "control_plane");
    expect(cps.filled).toBe(2);
    expect(cps.slots.map((slot) => slot.name)).toEqual(["cp-01", "cp-02", ""]);
    expect(cps.slots[2].state).toBe("empty");
  });

  it("flags an HA tier that shares an ESXi host, at pick time", () => {
    const plan = draftBlueprint({
      basics: HA_BASICS,
      picked: {
        "vm-2": { role: "control_plane" },
        "vm-3": { role: "control_plane" },
        "vm-4": { role: "control_plane" },
      },
      vms,
    });
    expect(plan.conflictHosts).toEqual(["esx-05"]);
    const cps = plan.tiers.find((tier) => tier.role === "control_plane");
    expect(cps.slots.filter((slot) => slot.tie).map((slot) => slot.name)).toEqual(["cp-02", "cp-03"]);
  });

  it("does not call a single machine on a host a conflict", () => {
    const plan = draftBlueprint({
      basics: HA_BASICS,
      picked: { "vm-2": { role: "control_plane" }, "vm-3": { role: "control_plane" } },
      vms,
    });
    expect(plan.conflictHosts).toEqual([]);
  });

  it("keeps manual hosts in the drawing", () => {
    const plan = draftBlueprint({
      basics: HA_BASICS,
      manualNodes: [{ role: "worker", hostname: "w-99", address: "10.0.0.99" }],
      vms,
    });
    const workers = plan.tiers.find((tier) => tier.role === "worker");
    expect(workers.slots[0]).toMatchObject({ name: "w-99", sub: "manual host", state: "set" });
    expect(workers.slots[1].state).toBe("empty");
  });
});

describe("preflightBlueprint", () => {
  const preflight = {
    status: "warn",
    nodes: [
      { nodeId: 1, hostname: "cp-01", address: "10.42.10.21", role: "control_plane", status: "pass", checks: [] },
      { nodeId: 2, hostname: "cp-02", address: "10.42.10.22", role: "control_plane", status: "warn", checks: [] },
      { nodeId: 3, hostname: "cp-03", address: "10.42.10.23", role: "control_plane", status: "fail", checks: [] },
    ],
  };

  it("stamps each slot with that machine's verdict", () => {
    const plan = preflightBlueprint(HA_BASICS, preflight);
    const cps = plan.tiers.find((tier) => tier.role === "control_plane");
    expect(cps.slots.map((slot) => slot.stamp)).toEqual(["ok", "warn", "bad"]);
    expect(plan.state).toBe("stamped");
  });

  it("keeps the ESXi placement from the pick step", () => {
    const hosts = hostByAddress({
      picked: { "vm-3": { role: "control_plane", address: "10.42.10.22" } },
      vms: [{ moid: "vm-3", name: "cp-02", esxiHost: "esx-05" }],
    });
    const plan = preflightBlueprint(HA_BASICS, preflight, hosts);
    const cps = plan.tiers.find((tier) => tier.role === "control_plane");
    expect(cps.slots[1].host).toBe("esx-05");
  });
});

describe("hostByAddress", () => {
  it("prefers an explicit address override over the Tools IP", () => {
    const map = hostByAddress({
      picked: { "vm-1": { role: "worker", address: "10.0.0.99" } },
      vms: [{ moid: "vm-1", name: "w-01", esxiHost: "esx-06", guestIp: "10.0.0.31" }],
    });
    expect(map).toEqual({ "10.0.0.99": "esx-06" });
  });
});

describe("buildBlueprint", () => {
  const build = {
    status: "building",
    topologyType: "stacked_ha",
    endpointMode: "managed_haproxy",
    vipAddress: "10.42.10.101",
    nodeCounts: { controlPlane: 3, worker: 2, loadbalancer: 2 },
    nodes: [
      { id: 1, role: "loadbalancer", hostname: "lb-01", status: "ready", isLbMaster: true },
      { id: 2, role: "loadbalancer", hostname: "lb-02", status: "ready" },
      { id: 3, role: "control_plane", hostname: "cp-01", status: "joined", isPrimaryCp: true },
      { id: 4, role: "control_plane", hostname: "cp-02", status: "preparing" },
      { id: 5, role: "control_plane", hostname: "cp-03", status: "pending" },
      { id: 6, role: "worker", hostname: "w-01", status: "pending" },
      { id: 7, role: "worker", hostname: "w-02", status: "pending" },
    ],
    steps: [
      { id: 10, phase: "loadbalancer", status: "completed", nodeId: 1 },
      { id: 11, phase: "join_cp", status: "running", nodeId: 4 },
    ],
  };

  it("distinguishes joined, working and waiting machines", () => {
    const plan = buildBlueprint(build);
    const cps = plan.tiers.find((tier) => tier.role === "control_plane");
    expect(cps.slots.map((slot) => slot.state)).toEqual(["joined", "live", "waiting"]);
    expect(cps.slots.map((slot) => slot.stamp)).toEqual(["ok", "live", null]);
    expect(plan.state).toBe("live");
  });

  it("does not dress an untouched machine as a failed one", () => {
    // A red control-plane box separated from a failure by a 17px badge is not
    // a distinction anyone reads while watching a build.
    const plan = buildBlueprint(build);
    const waiting = plan.tiers
      .flatMap((tier) => tier.slots)
      .filter((slot) => slot.state === "waiting");
    expect(waiting.map((slot) => slot.name)).toEqual(["cp-03", "w-01", "w-02"]);
    expect(waiting.every((slot) => slot.stamp === null)).toBe(true);
  });

  it("reads an unstarted build's machines as assigned, not waiting", () => {
    const plan = buildBlueprint({ ...build, status: "preflight_passed", steps: [] });
    expect(plan.tiers.flatMap((t) => t.slots).some((s) => s.state === "waiting")).toBe(false);
    expect(plan.state).toBe("stamped");
  });

  it("calls a stopped build stopped", () => {
    expect(buildBlueprint({ ...build, status: "failed" }).state).toBe("stopped");
    expect(buildBlueprint({ ...build, status: "cancelled" }).state).toBe("stopped");
  });

  it("marks the endpoint live once the load-balancer phase is done", () => {
    expect(buildBlueprint(build).bus.state).toBe("up");
    expect(buildBlueprint({ ...build, steps: [] }).bus.state).toBe("idle");
  });

  it("reads every machine as joined once the build completes", () => {
    const plan = buildBlueprint({ ...build, status: "completed" });
    expect(plan.state).toBe("built");
    expect(plan.tiers.flatMap((tier) => tier.slots).every((slot) => slot.state === "joined")).toBe(true);
  });

  it("shows a failed machine as failed, not merely unfinished", () => {
    const plan = buildBlueprint({
      ...build,
      nodes: build.nodes.map((node) => (node.id === 5 ? { ...node, status: "failed" } : node)),
    });
    const cps = plan.tiers.find((tier) => tier.role === "control_plane");
    expect(cps.slots[2]).toMatchObject({ state: "failed", stamp: "bad" });
  });

  it("hides the worker tier when a build has none", () => {
    const plan = buildBlueprint({
      ...build,
      topologyType: "single_cp",
      endpointMode: "manual_endpoint",
      controlPlaneEndpoint: "api.example:6443",
      nodeCounts: { controlPlane: 1, worker: 0, loadbalancer: 0 },
      nodes: build.nodes.filter((node) => node.role === "control_plane").slice(0, 1),
    });
    expect(plan.tiers.map((tier) => tier.role)).toEqual(["control_plane"]);
  });

  it("keeps an expected tier drawn even before its machines report", () => {
    // An HA build always owns two load balancers; the tier is part of the plan.
    const plan = buildBlueprint({ ...build, nodes: [] });
    expect(plan.tiers.map((tier) => tier.role)).toEqual(["loadbalancer", "control_plane"]);
    expect(plan.tiers[0].filled).toBe(0);
  });
});

describe("phases", () => {
  const base = {
    endpointMode: "managed_haproxy",
    nodeCounts: { controlPlane: 3, worker: 2, loadbalancer: 2 },
    addons: [{ id: "metallb", version: "0.16.1" }],
  };

  it("plans only the phases this build's shape will run", () => {
    expect(expectedPhases(base)).toHaveLength(10);
    expect(expectedPhases({ ...base, endpointMode: "external_lb" })).not.toContain("loadbalancer");
    expect(expectedPhases({ ...base, addons: [] })).not.toContain("addons");
    expect(
      expectedPhases({ ...base, nodeCounts: { controlPlane: 1, worker: 0, loadbalancer: 1 } })
    ).not.toContain("join_cp");
  });

  it("shows phases that have not started yet as todo", () => {
    const timeline = phaseTimeline({
      ...base,
      steps: [
        { id: 1, phase: "base_prep", status: "completed" },
        { id: 2, phase: "loadbalancer", status: "running" },
      ],
    });
    expect(timeline.map((cell) => cell.state).slice(0, 3)).toEqual(["done", "now", "todo"]);
    expect(timeline).toHaveLength(10);
  });

  it("derives the same rail from list data, where there are no steps", () => {
    const rail = railFromCurrentPhase({ ...base, status: "building", currentPhase: "join_cp" });
    expect(rail).toHaveLength(10);
    expect(rail.map((cell) => cell.state).slice(0, 7)).toEqual([
      "done", "done", "done", "done", "done", "now", "todo",
    ]);
  });

  it("marks the current phase failed on a failed build", () => {
    const rail = railFromCurrentPhase({ ...base, status: "failed", currentPhase: "join_cp" });
    expect(rail.find((cell) => cell.phase === "join_cp").state).toBe("fail");
  });

  it("shows nothing started when no phase has been reported yet", () => {
    const rail = railFromCurrentPhase({ ...base, status: "building", currentPhase: null });
    expect(rail.every((cell) => cell.state === "todo")).toBe(true);
  });

  it("calls a phase failed as soon as one of its steps fails", () => {
    const timeline = phaseTimeline({
      ...base,
      steps: [
        { id: 1, phase: "join_cp", status: "completed", nodeId: 1 },
        { id: 2, phase: "join_cp", status: "failed", nodeId: 2 },
      ],
    });
    expect(timeline.find((cell) => cell.phase === "join_cp").state).toBe("fail");
  });
});

describe("failurePoint", () => {
  it("names the furthest phase that failed and the machine it failed on", () => {
    const point = failurePoint({
      endpointMode: "managed_haproxy",
      nodeCounts: { controlPlane: 3, worker: 0, loadbalancer: 2 },
      addons: [],
      nodes: [{ id: 7, hostname: "cp-03", address: "10.0.0.23", role: "control_plane" }],
      steps: [
        { id: 1, phase: "base_prep", status: "completed" },
        { id: 2, phase: "join_cp", status: "failed", nodeId: 7, error: "etcd unhealthy" },
      ],
    });
    expect(point).toMatchObject({ phase: "join_cp", phaseLabel: "Join control planes" });
    expect(point.node.hostname).toBe("cp-03");
    expect(point.completedPhases).toBe(1);
  });

  it("is null while nothing has failed", () => {
    expect(failurePoint({ steps: [{ id: 1, phase: "init", status: "running" }] })).toBeNull();
  });
});

describe("growth", () => {
  const grown = {
    status: "building",
    resultClusterId: "custom-7",
    startedAt: "2026-07-22T08:00:00Z",
    growthStartedAt: "2026-07-27T11:50:00Z",
    buildSeconds: 18 * 60,
    finishedAt: "2026-07-22T08:18:00Z",
  };

  it("reads a re-running finished build as growth, not a fresh build", () => {
    expect(isGrowing(grown)).toBe(true);
    expect(isGrowing({ ...grown, resultClusterId: null })).toBe(false);
    expect(isGrowing({ ...grown, status: "completed" })).toBe(false);
  });

  it("clocks the growth run, not the age of the cluster", () => {
    expect(runStartedAt(grown)).toBe("2026-07-27T11:50:00Z");
    expect(runStartedAt({ ...grown, status: "completed" })).toBe("2026-07-22T08:00:00Z");
    // A first build has no growth clock to fall back on.
    expect(runStartedAt({ status: "building", startedAt: "2026-07-27T11:00:00Z" }))
      .toBe("2026-07-27T11:00:00Z");
  });

  it("keeps reporting the original build time after a growth run", () => {
    // Growth rewrites finishedAt; the banked duration is what the receipt shows.
    expect(buildDuration({ ...grown, status: "completed",
      finishedAt: "2026-07-27T12:04:00Z" })).toBe("18 min");
  });

  it("falls back to the timestamps when nothing was banked", () => {
    expect(buildDuration({
      startedAt: "2026-07-22T08:00:00Z", finishedAt: "2026-07-22T08:42:00Z",
    })).toBe("42 min");
    expect(buildDuration({ buildSeconds: 0, startedAt: null, finishedAt: null })).toBeNull();
  });

  it("formats a long build in hours", () => {
    expect(buildDuration({ buildSeconds: 3 * 3600 + 25 * 60 })).toBe("3 h 25 min");
  });
});

describe("groupBuilds", () => {
  it("puts what needs a person above what is finished", () => {
    const groups = groupBuilds([
      { id: 1, status: "completed" },
      { id: 2, status: "building" },
      { id: 3, status: "failed" },
      { id: 4, status: "draft" },
      { id: 5, status: "preflight_passed" },
    ]);
    expect(groups.inFlight.map((b) => b.id)).toEqual([2]);
    expect(groups.attention.map((b) => b.id)).toEqual([3, 4, 5]);
    expect(groups.done.map((b) => b.id)).toEqual([1]);
  });
});

describe("deriveReadiness", () => {
  const infra = {
    vsphere: [{ id: 1, name: "areeba DC", lastConnectionStatus: "ok", lastTestedAt: isoAgo(2) }],
    credentials: [{ id: 1, authMethod: "key", sudoMode: "nopasswd" }],
    profiles: [{
      id: 1, name: "ops-nodes", credentialId: 1, hostKeyPolicy: "pinned",
      lastTestStatus: "ok", lastTestAt: isoAgo(3),
    }],
    buildProfiles: [{ id: 1, name: "areeba nexus", repoMode: "mirror", k8sPkgRepoUrl: "https://pkgs/" }],
  };
  const catalog = [
    { id: "metrics-server", versions: ["0.7.2"], bundledVersions: ["0.7.2"] },
    { id: "metallb", versions: ["0.16.1"], bundledVersions: ["0.16.1"] },
  ];

  it("is ready when every source has a fresh proof", () => {
    const readiness = deriveReadiness({
      builds: [], infra, addonCatalog: catalog, canManageInfra: true, now: NOW,
    });
    expect(readiness.state).toBe("ready");
    expect(readiness.segments.map((s) => s.key)).toEqual(["vsphere", "ssh", "sources", "builds"]);
    expect(readiness.segments[0]).toMatchObject({ state: "ok", sub: "tested 2 h ago" });
  });

  it("asks for a re-test rather than showing a stale proof as green", () => {
    const readiness = deriveReadiness({
      builds: [],
      infra: {
        ...infra,
        vsphere: [{ id: 1, name: "areeba DC", lastConnectionStatus: "ok", lastTestedAt: isoAgo(9 * 24) }],
      },
      addonCatalog: catalog, canManageInfra: true, now: NOW,
    });
    expect(readiness.state).toBe("attention");
    expect(readiness.segments[0].state).toBe("warn");
    expect(readiness.segments[0].fix).toContain("re-test");
  });

  it("blocks when there is no way in at all", () => {
    const readiness = deriveReadiness({
      builds: [], infra: { ...infra, profiles: [] },
      addonCatalog: catalog, canManageInfra: true, now: NOW,
    });
    expect(readiness.state).toBe("blocked");
    expect(readiness.headline).toBe("Not ready to build");
    expect(readiness.sub).toContain("ssh reach");
  });

  it("reports a failed test as worse than an old one", () => {
    const readiness = deriveReadiness({
      builds: [],
      infra: {
        ...infra,
        profiles: [{ id: 1, name: "ops-nodes", credentialId: 1, hostKeyPolicy: "tofu", lastTestStatus: "failed", lastTestAt: isoAgo(1) }],
      },
      addonCatalog: catalog, canManageInfra: true, now: NOW,
    });
    expect(readiness.segments[1]).toMatchObject({ state: "bad" });
  });

  it("flags a source profile that cannot work as configured", () => {
    const readiness = deriveReadiness({
      builds: [],
      infra: { ...infra, buildProfiles: [{ id: 1, name: "offline", repoMode: "offline" }] },
      addonCatalog: catalog, canManageInfra: true, now: NOW,
    });
    const sources = readiness.segments.find((segment) => segment.key === "sources");
    expect(sources).toMatchObject({ state: "warn", fix: "Bundle path is missing." });
  });

  it("hides infrastructure segments a user cannot see", () => {
    const readiness = deriveReadiness({
      builds: [], infra, addonCatalog: catalog, canManageInfra: false, now: NOW,
    });
    expect(readiness.segments.map((s) => s.key)).toEqual(["sources", "builds"]);
  });

  it("reports a running build as live", () => {
    const readiness = deriveReadiness({
      builds: [{ id: 1, name: "areeba-uat-02", status: "building" }],
      infra, addonCatalog: catalog, canManageInfra: true, now: NOW,
    });
    const builds = readiness.segments.find((segment) => segment.key === "builds");
    expect(builds).toMatchObject({ state: "live", value: "1 in flight", sub: "areeba-uat-02" });
  });
});

describe("preferredSources", () => {
  // The API orders these by name, so "first match" is not "healthiest".
  const infra = {
    vsphere: [
      { id: 1, name: "areeba DC", lastConnectionStatus: "ok", lastTestedAt: isoAgo(2) },
      { id: 2, name: "aaa old DC", lastConnectionStatus: "ok", lastTestedAt: isoAgo(20 * 24) },
    ],
    profiles: [
      { id: 10, name: "dmz-nodes", lastTestStatus: "ok", lastTestAt: isoAgo(12 * 24) },
      { id: 11, name: "ops-nodes", lastTestStatus: "ok", lastTestAt: isoAgo(3) },
    ],
    buildProfiles: [
      { id: 20, name: "air-gapped", repoMode: "offline" },
      { id: 21, name: "areeba nexus", repoMode: "mirror", k8sPkgRepoUrl: "https://p/" },
    ],
  };

  it("prefers the freshest proof, not the first row", () => {
    const picked = preferredSources(infra, NOW);
    expect(picked.vcenter.name).toBe("areeba DC");
    expect(picked.route.name).toBe("ops-nodes");
  });

  it("never defaults to a source profile that cannot work as configured", () => {
    expect(preferredSources(infra, NOW).buildProfile.name).toBe("areeba nexus");
  });

  it("falls back to an untested record rather than nothing", () => {
    const picked = preferredSources({
      vsphere: [{ id: 3, name: "brand new" }],
      profiles: [{ id: 4, name: "brand new route" }],
      buildProfiles: [],
    }, NOW);
    expect(picked.vcenter.name).toBe("brand new");
    expect(picked.route.name).toBe("brand new route");
    // No usable profile means internet defaults, which is a legitimate choice.
    expect(picked.buildProfile).toBeNull();
  });

  it("returns nulls when nothing is configured", () => {
    expect(preferredSources({}, NOW)).toEqual({ vcenter: null, route: null, buildProfile: null });
  });
});

describe("sshPosture", () => {
  it("says what the route actually is", () => {
    expect(sshPosture(
      { credentialId: 1, hostKeyPolicy: "pinned" },
      [{ id: 1, authMethod: "key", sudoMode: "nopasswd" }],
    )).toBe("key auth · passwordless sudo · host keys pinned");
  });

  it("degrades gracefully when the credential is not loaded", () => {
    expect(sshPosture({ credentialId: 9, hostKeyPolicy: "tofu" }, [])).toBe("trust on first use");
  });
});

describe("sourceProfileSummary", () => {
  it("answers the three questions for a mirror plus registry proxy", () => {
    expect(sourceProfileSummary({
      name: "areeba nexus", repoMode: "mirror",
      k8sPkgRepoUrl: "https://packages.areeba.local/kubernetes/v{minor}/deb/",
      k8sImageRegistry: "nexus.areeba.local:5000/kubernetes",
      extraCaConfigured: true,
    })).toMatchObject({
      packages: "Internal package mirror",
      images: "A registry proxy",
      imagesDetail: "nexus.areeba.local:5000/kubernetes",
      obstacles: ["No outbound proxy", "1 trusted CA certificate"],
      incomplete: "",
    });
  });

  it("describes the no-profile case as internet defaults", () => {
    expect(sourceProfileSummary(null)).toMatchObject({
      packages: "Upstream internet repositories",
      packagesDetail: "dev and test only",
    });
  });

  it("names an incomplete profile's missing piece", () => {
    expect(sourceProfileSummary({ repoMode: "mirror" }).incomplete).toBe("Mirror URL is missing.");
  });
});

describe("add-on provenance", () => {
  const entry = {
    id: "metallb",
    versions: ["0.16.1", "0.14.8"],
    bundledVersions: ["0.16.1"],
    manifestDigests: [{ file: "metallb-native.yaml", sha256: "bf25feebb7582ca7df845efd52ffbc2960d6cbf4cfc972f47fded9f788b67f0b" }],
  };

  it("shortens a digest to something recognisable", () => {
    expect(shortDigest(entry.manifestDigests[0].sha256)).toBe("sha256:bf25…7f0b");
    expect(shortDigest("abc")).toBe("abc");
  });

  it("says whether the selected version can install with no internet", () => {
    expect(addonProvenance(entry, "0.16.1")).toMatchObject({ bundled: true, text: "bundled" });
    expect(addonProvenance(entry, "0.14.8")).toMatchObject({ bundled: false, text: "downloads on build" });
  });

  it("counts bundle coverage across the catalog", () => {
    expect(bundleCoverage([entry, { id: "x", bundledVersions: [] }])).toEqual({
      total: 2, bundled: 1, complete: false,
    });
    expect(bundleCoverage([entry])).toMatchObject({ complete: true });
  });
});

describe("Kubernetes-version compatibility", () => {
  const calico = {
    id: "calico",
    displayName: "Calico",
    versions: ["3.32.1", "3.28.2", "3.27.4"],
    k8sMinorsByVersion: {
      "3.32.1": ["1.34", "1.35", "1.36"],
      "3.28.2": ["1.29", "1.30", "1.31", "1.32"],
      "3.27.4": ["1.29", "1.30", "1.31", "1.32"],
    },
  };
  const flannel = {
    id: "flannel",
    displayName: "Flannel",
    versions: ["0.25.6"],
    k8sMinorsByVersion: { "0.25.6": ["1.29", "1.30", "1.31", "1.32"] },
  };

  it("reads the minor out of a patch version", () => {
    expect(k8sMinorOf("1.32.4")).toBe("1.32");
    expect(k8sMinorOf("v1.36.0")).toBe("1.36");
    expect(k8sMinorOf("1.32")).toBe("1.32");
    expect(k8sMinorOf("garbage")).toBe("");
    expect(k8sMinorOf(undefined)).toBe("");
  });

  it("keeps only the versions validated on the chosen release", () => {
    expect(versionsForK8s(calico, "1.36.0")).toEqual(["3.32.1"]);
    expect(versionsForK8s(calico, "1.32.13")).toEqual(["3.28.2", "3.27.4"]);
    expect(versionsForK8s(flannel, "1.36.0")).toEqual([]);
  });

  it("defaults to the newest compatible version, not the newest overall", () => {
    expect(defaultVersionForK8s(calico, "1.32.13")).toBe("3.28.2");
    expect(defaultVersionForK8s(calico, "1.36.0")).toBe("3.32.1");
    expect(defaultVersionForK8s(flannel, "1.36.0")).toBe("");
  });

  it("hides plugins that cannot serve the chosen release", () => {
    expect(cniPluginsForK8s([calico, flannel], "1.36.0").map((p) => p.id))
      .toEqual(["calico"]);
    expect(cniPluginsForK8s([calico, flannel], "1.32.13").map((p) => p.id))
      .toEqual(["calico", "flannel"]);
  });

  it("shows everything when the backend sends no matrix", () => {
    // An older backend has no k8sMinorsByVersion; emptying the picker would be
    // worse than showing an unfiltered list.
    const legacy = { id: "calico", versions: ["3.28.2"] };
    expect(versionsForK8s(legacy, "1.36.0")).toEqual(["3.28.2"]);
    expect(cniPluginsForK8s([legacy], "1.36.0")).toHaveLength(1);
  });

  it("picks the digest set matching the selected version", () => {
    const entry = {
      versions: ["0.9.0", "0.7.2"],
      bundledVersions: ["0.9.0", "0.7.2"],
      manifestDigestsByVersion: {
        "0.9.0": [{ file: "components.yaml", sha256: "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b" }],
        "0.7.2": [{ file: "components.yaml", sha256: "f103539a54ed72efe66616afc74a8bfaed651703cb3918797599046af5617441" }],
      },
    };
    expect(addonProvenance(entry, "0.9.0").digest).toBe("sha256:1cec…c79b");
    expect(addonProvenance(entry, "0.7.2").digest).toBe("sha256:f103…7441");
  });
});

// ---------------------------------------------------------------------------
// Bringing workloads from an existing cluster
// ---------------------------------------------------------------------------

describe("workload selection", () => {
  const api = { namespace: "core", kind: "Deployment", name: "api" };
  const ui = { namespace: "web", kind: "StatefulSet", name: "ui" };

  it("toggles one workload on and off", () => {
    const once = toggleWorkload([], api);
    expect(once).toEqual([api]);
    expect(toggleWorkload(once, api)).toEqual([]);
  });

  it("narrowing a whole namespace to one workload drops the whole marker", () => {
    // Otherwise the user clicks one deployment and still gets all twenty.
    const whole = toggleWholeNamespace([], "core");
    expect(isWholeNamespaceSelected(whole, "core")).toBe(true);
    const narrowed = toggleWorkload(whole, api);
    expect(isWholeNamespaceSelected(narrowed, "core")).toBe(false);
    expect(narrowed).toEqual([api]);
  });

  it("unticking one row of a whole namespace leaves the rest, named", () => {
    // The gesture means "not that one" — narrowing to only the clicked row
    // would be the opposite of what the click says.
    const all = [
      { kind: "Deployment", name: "api" },
      { kind: "Deployment", name: "web" },
      { kind: "CronJob", name: "nightly" },
    ];
    const whole = toggleWholeNamespace([], "core");
    const expanded = expandNamespaceSelection(whole, "core", all);
    expect(expanded).toHaveLength(3);
    const remaining = toggleWorkload(expanded, { namespace: "core", ...all[1] });
    expect(remaining.map((item) => item.name)).toEqual(["api", "nightly"]);
    expect(isWholeNamespaceSelected(remaining, "core")).toBe(false);
  });

  it("expanding leaves namespaces that were not selected whole alone", () => {
    expect(expandNamespaceSelection([api], "core", [{ kind: "Deployment", name: "x" }]))
      .toEqual([api]);
  });

  it("selecting a whole namespace replaces the individual picks in it", () => {
    const items = toggleWholeNamespace([api, ui], "core");
    expect(items).toEqual([ui, { namespace: "core", kind: "Namespace", name: "" }]);
    // Other namespaces are untouched.
    expect(selectedInNamespace(items, "web")).toEqual([ui]);
  });

  it("clearing a whole namespace clears everything in it", () => {
    const whole = toggleWholeNamespace([ui], "core");
    expect(toggleWholeNamespace(whole, "core")).toEqual([ui]);
  });

  it("removes a workload by identity, not by position", () => {
    expect(removeWorkload([api, ui], { ...api })).toEqual([ui]);
    expect(removeWorkload([api, ui], { ...api, name: "other" })).toEqual([api, ui]);
  });

  it("summarizes what will be copied", () => {
    expect(workloadSelectionSummary([])).toBe("Nothing selected");
    expect(workloadSelectionSummary([api])).toBe("1 workload across 1 namespace");
    expect(workloadSelectionSummary([
      api, ui, { namespace: "pay", kind: "Namespace", name: "" },
    ])).toBe("1 whole namespace · 2 workloads across 3 namespaces");
  });

  it("drops the namespace clause where it would read as nonsense", () => {
    // Inside a button: "Copy 1 whole namespace across 1 namespace into prod".
    const whole = [{ namespace: "pay", kind: "Namespace", name: "" }];
    expect(workloadSelectionSummary(whole)).toBe("1 whole namespace");
    expect(workloadSelectionSummary([api, ui], { short: true }))
      .toBe("2 workloads");
  });
});

describe("workloadPlanVerdict", () => {
  const plan = (extra = {}) => ({
    registryConnectionId: 2,
    workloads: [
      { namespace: "core", kind: "Deployment", name: "api", imageStatus: "ok" },
      { namespace: "core", kind: "Deployment", name: "web", imageStatus: "missing" },
    ],
    missingWorkloads: [{
      namespace: "core", kind: "Deployment", name: "web",
      missingImages: ["registry.local/web:2"],
    }],
    imageChecks: [{ image: "registry.local/api:1", status: "found", registry: "nexus" }],
    support: { ConfigMap: 2, Secret: 1 },
    counts: { workloads: 2, images: 3, missingImages: 1 },
    warnings: ["node ports are not copied"],
    missing: [],
    ...extra,
  });

  it("warns about missing images without ever blocking", () => {
    const verdict = workloadPlanVerdict(plan());
    expect(verdict.tone).toBe("warn");
    expect(verdict.missing).toHaveLength(1);
    expect(verdict.okCount).toBe(1);
    expect(verdict.supportCount).toBe(3);
    expect(verdict.headline).toBe("1 workload would start without an image in nexus");
    expect(verdict).not.toHaveProperty("blocking");
  });

  it("says so plainly when no registry was chosen", () => {
    const verdict = workloadPlanVerdict(plan({
      registryConnectionId: null,
      missingWorkloads: [],
      counts: { workloads: 2, images: 3, missingImages: 0 },
    }));
    expect(verdict.checked).toBe(false);
    expect(verdict.tone).toBe("plain");
    expect(verdict.headline).toContain("pick a registry");
  });

  it("is good when everything is present", () => {
    const verdict = workloadPlanVerdict(plan({
      workloads: [{ namespace: "core", kind: "Deployment", name: "api", imageStatus: "ok" }],
      missingWorkloads: [],
      counts: { workloads: 1, images: 1, missingImages: 0 },
    }));
    expect(verdict.tone).toBe("good");
    expect(verdict.headline).toBe("Every image of all 1 workload is in the registry");
  });

  it("survives an empty plan", () => {
    const verdict = workloadPlanVerdict(null);
    expect(verdict.total).toBe(0);
    expect(verdict.missing).toEqual([]);
  });

  it("names each image status in words", () => {
    expect(imageStatusText("missing")).toBe("no image in the registry");
    expect(imageStatusText("ok")).toBe("in the registry");
    expect(imageStatusText("weird")).toBe("weird");
  });
});

describe("workloadReceipt", () => {
  it("is null until something has actually landed", () => {
    expect(workloadReceipt({ workloads: { itemCount: 2 } })).toBeNull();
  });

  it("accumulates every run, and reports what is not ready", () => {
    const receipt = workloadReceipt({
      workloads: { appliedRuns: 2 },
      workloadSelection: {
        applied: [
          {
            sourceClusterName: "areeba-prod-01", namespaces: ["core"],
            workloads: [{ namespace: "core", kind: "Deployment", name: "api" }],
            support: { ConfigMap: 1 }, notReady: [], skipped: [],
          },
          {
            sourceClusterName: "areeba-prod-01", namespaces: ["pay"],
            workloads: [{ namespace: "pay", kind: "CronJob", name: "nightly" }],
            support: { Secret: 2 }, notReady: ["pay/nightly"], skipped: ["pay/Deployment gone"],
            volumes: [{ claim: "pay/data", source: "reuse", pv: "kubesight-pay-data",
                        target: "10.9.9.9:/vol/pay", capacity: "20Gi", readOnly: false }],
            unboundClaims: [],
          },
        ],
      },
    });
    expect(receipt.source).toBe("areeba-prod-01");
    expect(receipt.namespaces).toEqual(["core", "pay"]);
    expect(receipt.workloads).toHaveLength(2);
    expect(receipt.runs).toBe(2);
    expect(receipt.notReady).toEqual(["pay/nightly"]);
    expect(receipt.supportCount).toBe(2);
    expect(receipt.skipped).toEqual(["pay/Deployment gone"]);
    // Volumes are the latest run's only: a claim has one backing at a time.
    expect(receipt.volumes).toEqual([{
      claim: "pay/data", source: "reuse", pv: "kubesight-pay-data",
      target: "10.9.9.9:/vol/pay", capacity: "20Gi", readOnly: false,
    }]);
  });
});

describe("expectedPhases with a workload copy", () => {
  it("plans the workloads phase only when something is selected", () => {
    const base = {
      endpointMode: "manual_endpoint", nodeCounts: { controlPlane: 1, worker: 1 },
      addons: [],
    };
    expect(expectedPhases(base)).not.toContain("workloads");
    expect(expectedPhases({ ...base, workloads: { itemCount: 3 } }))
      .toContain("workloads");
  });
});

// ---------------------------------------------------------------------------
// Where copied volume claims land
// ---------------------------------------------------------------------------

describe("storage decisions", () => {
  const claim = (key, extra = {}) => ({
    key,
    namespace: key.split("/")[0],
    name: key.split("/")[1],
    capacity: "5Gi",
    accessModes: ["ReadWriteOnce"],
    storageClassName: "",
    reusable: false,
    reuseBlocked: "it is not bound to a volume in the source cluster",
    sourceTarget: "",
    ...extra,
  });
  const plan = (...claims) => ({ storage: { claims } });
  const nfs = {
    ...emptyStorage(), default: "fresh",
    nfsServer: "10.4.1.20", nfsExportRoot: "/exports/ks",
  };

  it("falls back to the copy-wide default", () => {
    expect(storageDecision(nfs, "core/api-data").source).toBe("fresh");
    const narrowed = setStorageDecision(nfs, "core/api-data", { source: "none" });
    expect(storageDecision(narrowed, "core/api-data").source).toBe("none");
    expect(storageDecision(narrowed, "core/other").source).toBe("fresh");
  });

  it("read-only only survives on a reused export", () => {
    // It means "do not write to somebody else's data"; on a directory we just
    // created for this cluster it is nonsense.
    const reused = setStorageDecision(nfs, "core/db", { source: "reuse", readOnly: true });
    expect(storageDecision(reused, "core/db").readOnly).toBe(true);
    const fresh = setStorageDecision(reused, "core/db", { source: "fresh" });
    expect(storageDecision(fresh, "core/db").readOnly).toBe(false);
  });

  it("setting all clears the per-claim overrides that would win", () => {
    const mixed = setStorageDecision(nfs, "core/api-data", { source: "none" });
    const all = setAllStorageDecisions(mixed, ["core/api-data", "core/db"], "class");
    expect(all.default).toBe("class");
    expect(storageDecision(all, "core/api-data").source).toBe("class");
  });

  it("previews the target without asking the server", () => {
    const rows = storageRows(plan(claim("core/api-data")), nfs);
    expect(rows[0].target).toBe("10.4.1.20:/exports/ks/core/api-data");
    expect(rows[0].error).toBe("");
  });

  it("a reuse row shows the source's own path", () => {
    const row = claim("core/db", { reusable: true, sourceTarget: "10.9.9.9:/vol/db" });
    const rows = storageRows(plan(row), { ...nfs, default: "reuse" });
    expect(rows[0].target).toBe("10.9.9.9:/vol/db");
  });

  it("refuses reuse where the source volume is not NFS", () => {
    const row = claim("core/db", {
      reusable: false, reuseBlocked: "its volume in the source cluster is backed by the CSI driver x, not NFS",
    });
    const rows = storageRows(plan(row), { ...nfs, default: "reuse" });
    expect(rows[0].error).toContain("Cannot reuse");
    expect(storageErrors(rows)).toHaveLength(1);
  });

  it("asks for the fields each decision actually needs", () => {
    const rows = storageRows(plan(claim("core/api-data")), { ...nfs, nfsServer: "" });
    expect(rows[0].error).toContain("NFS server");
    expect(storageNeeds(rows)).toEqual({ nfs: true, storageClass: false, reuse: false });

    const classy = storageRows(plan(claim("core/api-data")), {
      ...emptyStorage(), default: "class",
    });
    expect(classy[0].error).toBe("Name a StorageClass.");
    expect(storageNeeds(classy).storageClass).toBe(true);
  });

  it("a claim that names its own StorageClass needs no answer", () => {
    const rows = storageRows(
      plan(claim("core/api-data", { storageClassName: "fast" })),
      { ...emptyStorage(), default: "class" }
    );
    expect(rows[0].error).toBe("");
    expect(rows[0].target).toBe("fast");
  });

  it("summarises a mixed plan", () => {
    const storage = setAllStorageDecisions(
      setStorageDecision(nfs, "core/cache", { source: "none" }),
      [], "fresh"
    );
    const rows = storageRows(
      plan(claim("core/api-data"), claim("core/cache")), storage
    );
    expect(storageSummary(rows)).toBe("1 new volume · 1 left pending");
    expect(storageSummary([])).toBe("");
  });

  it("has no rows when nothing has volume claims", () => {
    expect(storageRows({ storage: { claims: [] } }, nfs)).toEqual([]);
    expect(storageRows(null, nfs)).toEqual([]);
  });
});
