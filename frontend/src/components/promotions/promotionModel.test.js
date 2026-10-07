import { describe, expect, it } from "vitest";

import {
  busiestGate,
  filterApps,
  formatAge,
  formatMinutes,
  gateQueue,
  groupApps,
  groupByDay,
  planRelease,
  releasePayload,
  sortRows,
  tagOf,
  versionChoices,
} from "./promotionModel.js";

const ENVS = [
  { id: 1, name: "Dev" },
  { id: 2, name: "SIT" },
  { id: 3, name: "UAT" },
  { id: 4, name: "Pre-prod" },
];

const wl = (image, extra = {}) => ({
  clusterId: "c1",
  namespace: "ns",
  kind: "Deployment",
  name: "api",
  container: "api",
  image,
  tag: tagOf(image),
  state: "healthy",
  ...extra,
});

const cell = (environmentId, images, state = "healthy") => ({
  environmentId,
  images,
  tags: images.map(tagOf),
  state: images.length ? state : "empty",
  workloads: images.map((image) => wl(image, { state })),
  drift: [],
});

const app = (name, versions, extra = {}) => ({
  key: `repo/${name}`,
  name,
  repository: `repo/${name}`,
  system: extra.system || "cards",
  team: extra.team || null,
  namespaces: ["cards-dev"],
  lag: extra.lag ?? 1,
  drift: Boolean(extra.drift),
  cells: versions.map((v, i) => cell(i + 1, v ? [`repo/${name}:${v}`] : [], extra.states?.[i])),
  steps: [1, 2, 3].map((to) => ({
    fromEnvironmentId: to,
    toEnvironmentId: to + 1,
    state: extra.stepStates?.[to - 1] || "in_sync",
    image: versions[to - 1] ? `repo/${name}:${versions[to - 1]}` : null,
    passedAt: extra.passedAt?.[to - 1] || null,
  })),
});

const PAYMENTS = app("payments", ["2.9.0", "2.8.4", "2.8.1", "2.8.1"], {
  stepStates: ["ready", "ready", "in_sync"],
  lag: 2,
});
const LEDGER = app("ledger", ["1.21.0", "1.21.0", "1.20.2", null], {
  system: "core",
  stepStates: ["in_sync", "ready", "not_deployed"],
  drift: true,
  states: [undefined, "progressing"],
});

describe("promotionModel", () => {
  it("reads tags and ages", () => {
    expect(tagOf("ghcr.io/mock/payments:v2.9.0")).toBe("v2.9.0");
    expect(tagOf("nexus.local:8443/team/api")).toBe("nexus.local:8443/team/api");
    const now = new Date("2026-10-06T12:00:00Z").getTime();
    expect(formatAge("2026-10-06T11:30:00Z", now)).toBe("30 min");
    expect(formatAge("2026-10-03T12:00:00Z", now)).toBe("3 d");
    expect(formatMinutes(90)).toBe("1 h 30 min");
  });

  it("picks the gate with the most ready applications", () => {
    const gates = [
      { fromEnvironmentId: 1, toEnvironmentId: 2, counts: { ready: 3 } },
      { fromEnvironmentId: 2, toEnvironmentId: 3, counts: { ready: 9 } },
    ];
    expect(busiestGate(gates).toEnvironmentId).toBe(3);
  });

  it("filters an estate by state, system and search", () => {
    const apps = [PAYMENTS, LEDGER];
    expect(filterApps(apps, { filter: "drift" }).map((a) => a.name)).toEqual(["ledger"]);
    expect(filterApps(apps, { filter: "rolling" }).map((a) => a.name)).toEqual(["ledger"]);
    expect(filterApps(apps, { filter: "gaps" }).map((a) => a.name)).toEqual(["ledger"]);
    expect(filterApps(apps, { systems: ["cards"] }).map((a) => a.name)).toEqual(["payments"]);
    expect(filterApps(apps, { search: "ledg" })).toHaveLength(1);
  });

  it("groups by system", () => {
    const groups = groupApps([PAYMENTS, LEDGER], "system");
    expect(groups.map((g) => [g.label, g.items.length])).toEqual([
      ["cards", 1],
      ["core", 1],
    ]);
  });

  it("builds a gate queue and sorts the longest-waiting first", () => {
    const rows = gateQueue([PAYMENTS, LEDGER], 1);
    expect(rows.map((r) => r.step.state)).toEqual(["ready", "ready"]);
    rows[0].step.passedAt = "2026-10-05T00:00:00Z";
    rows[1].step.passedAt = "2026-10-01T00:00:00Z";
    expect(sortRows(rows, "waiting").map((r) => r.app.name)).toEqual(["ledger", "payments"]);
  });

  it("offers the nearest version first and names what a far one skips", () => {
    const choices = versionChoices(PAYMENTS, ENVS, 2);
    expect(choices.map((c) => [c.tag, c.environmentName, c.distance, c.skips])).toEqual([
      ["2.8.4", "SIT", 1, []],
      ["2.9.0", "Dev", 2, ["SIT"]],
    ]);
  });

  it("plans a release: workloads, clusters needing approval, exceptions", () => {
    const rows = new Map(gateQueue([PAYMENTS, LEDGER], 1).map((r) => [r.app.key, r]));
    const picks = new Map([
      [PAYMENTS.key, { image: "repo/payments:2.8.4" }],
      [LEDGER.key, { image: "repo/ledger:1.21.0", exception: true, skips: ["SIT"] }],
    ]);
    const plan = planRelease(picks, rows, { c1: { requiredApprovals: 1 } });
    expect(plan.items).toHaveLength(2);
    expect(plan.workloads).toBe(2);
    expect(plan.exceptions).toBe(1);
    expect(plan.needsApproval).toBe(true);
    const body = releasePayload(plan, { environmentId: 3, name: "UAT drop", exceptionReason: "hotfix" });
    expect(body.items[0]).toEqual({
      name: "payments",
      image: "repo/payments:2.8.4",
      targets: [{ clusterId: "c1", namespace: "ns", kind: "Deployment", name: "api", container: "api" }],
    });
    expect(body.exceptionReason).toBe("hotfix");
  });

  it("drops an app whose workloads already run the picked version", () => {
    const rows = new Map(gateQueue([PAYMENTS], 2).map((r) => [r.app.key, r]));
    const plan = planRelease(new Map([[PAYMENTS.key, { image: "repo/payments:2.8.1" }]]), rows, {});
    expect(plan.items).toEqual([]);
  });

  it("groups activity by day", () => {
    const now = new Date("2026-10-06T12:00:00Z");
    const groups = groupByDay(
      [
        { id: 1, createdAt: "2026-10-06T10:00:00Z" },
        { id: 3, createdAt: "2026-10-05T09:00:00Z" },
      ],
      now
    );
    expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday"]);
  });
});
