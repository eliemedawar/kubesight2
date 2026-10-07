import { describe, expect, it } from "vitest";

import {
  boardStatus,
  bySystem,
  countStates,
  departureApps,
  fmtWhen,
  nextDeparture,
  promotableItems,
} from "./timetableModel.js";

const ENVS = [
  { id: 1, name: "Dev", minSoakMinutes: 0 },
  { id: 2, name: "SIT", minSoakMinutes: 240 },
  { id: 3, name: "UAT", minSoakMinutes: 0 },
];

const wl = (image) => ({ clusterId: "c1", namespace: "ns", kind: "Deployment", name: "api", container: "api", image, tag: image.split(":")[1], state: "healthy" });
const cell = (images) => ({ images, tags: images.map((i) => i.split(":")[1]), workloads: images.map(wl), state: images.length ? "healthy" : "empty", drift: [] });

const app = (name, system, state, extra = {}) => ({
  key: `r/${name}`,
  name,
  repository: `r/${name}`,
  system,
  cells: [cell([`r/${name}:3`]), cell([`r/${name}:2`]), cell([`r/${name}:1`])],
  steps: [
    { state: "in_sync", image: `r/${name}:3`, tag: "3", targets: [] },
    { state, image: `r/${name}:2`, tag: "2", targets: [{ ...wl(`r/${name}:1`) }], ...extra },
  ],
});

const OVERVIEW = {
  environments: ENVS,
  apps: [
    app("api", "cards", "ready"),
    app("worker", "cards", "soaking", { soakMinutesLeft: 10 }),
    app("batch", "ledger", "soaking", { soakMinutesLeft: 90 }),
    app("web", "ledger", "waiting"),
    app("rules", "risk", "blocked"),
    app("sync", "risk", "in_sync"),
  ],
};

describe("timetableModel", () => {
  it("works out who is in an open departure", () => {
    const now = Date.parse("2026-10-06T13:13:00Z");
    const dep = { kind: "scheduled", toEnvironmentId: 3, cutoffAt: "2026-10-06T13:45:00Z", excluded: ["r/rules"] };
    const rows = departureApps(dep, OVERVIEW, now);
    const states = Object.fromEntries(rows.map((r) => [r.app.name, r.state]));
    expect(states).toEqual({ api: "eligible", worker: "soaking", batch: "late", web: "late", rules: "moved" });
    expect(countStates(rows)).toEqual({ eligible: 1, soaking: 1, late: 2, moved: 1 });
  });

  it("promotes only the eligible ones, with clean targets", () => {
    const rows = departureApps({ kind: "ondemand", toEnvironmentId: 3, excluded: [] }, OVERVIEW);
    const items = promotableItems(rows);
    expect(items).toEqual([
      { name: "api", image: "r/api:2", targets: [{ clusterId: "c1", namespace: "ns", kind: "Deployment", name: "api", container: "api" }] },
    ]);
  });

  it("groups by system with problems after the eligible", () => {
    const rows = departureApps({ kind: "ondemand", toEnvironmentId: 3, excluded: [] }, OVERVIEW);
    expect(bySystem(rows).map((g) => [g.name, g.items.map((r) => r.state)])).toEqual([
      ["cards", ["eligible", "soaking"]],
      ["ledger", ["soaking", "late"]],
      ["risk", ["blocked"]],
    ]);
  });

  it("reads a closed release from its items", () => {
    const dep = {
      kind: "release",
      toEnvironmentId: 3,
      release: {
        departsAt: null,
        items: [
          { repository: "r/api", name: "api", tag: "2", fromTags: ["1"], targets: [{ status: "applied" }] },
          { repository: "r/web", name: "web", tag: "2", fromTags: ["1"], targets: [{ status: "refused", message: "nope" }] },
        ],
      },
    };
    const rows = departureApps(dep, OVERVIEW);
    expect(rows.map((r) => [r.app.name, r.state, r.detail])).toEqual([
      ["api", "promoted", ""],
      ["web", "refused", "nope"],
    ]);
  });

  it("names statuses and times", () => {
    expect(boardStatus({ kind: "ondemand", status: "boarding" }).text).toBe("OPEN");
    expect(boardStatus({ kind: "release", status: "failed" }).tone).toBe("bad");
    expect(fmtWhen("2026-10-06T14:00:00Z", "UTC", "2026-10-06T13:13:00Z")).toBe("14:00");
    expect(fmtWhen("2026-10-08T14:00:00Z", "UTC", "2026-10-06T13:13:00Z")).toBe("Thu 14:00");
  });

  it("finds the next departure of a hop after a moment", () => {
    const tt = {
      departures: [
        { kind: "scheduled", toEnvironmentId: 3, status: "boarding", departsAt: "2026-10-06T14:00:00Z", cutoffAt: "2026-10-06T13:45:00Z" },
        { kind: "scheduled", toEnvironmentId: 3, status: "scheduled", departsAt: "2026-10-08T14:00:00Z", cutoffAt: "2026-10-08T13:45:00Z" },
      ],
    };
    expect(nextDeparture(tt, 3, Date.parse("2026-10-06T13:50:00Z")).departsAt).toBe("2026-10-08T14:00:00Z");
    expect(nextDeparture(tt, 3, Date.parse("2026-10-06T13:00:00Z")).departsAt).toBe("2026-10-06T14:00:00Z");
  });
});
