import { describe, expect, it } from "vitest";
import {
  bucketAlertHistory,
  formatDurationShort,
  groupAlerts,
  parseAlertTime,
  resolvedStats,
  severityInfo,
  severitySeries,
} from "./alertFeed.js";

const NOW = Date.parse("2026-07-09T15:40:00Z");

describe("severityInfo", () => {
  it("ranks critical < warning < info and maps tones", () => {
    expect(severityInfo("critical")).toEqual({ rank: 0, tone: "danger", label: "critical" });
    expect(severityInfo("warning")).toEqual({ rank: 1, tone: "warn", label: "warning" });
    expect(severityInfo("info").rank).toBe(2);
    expect(severityInfo(undefined).tone).toBe("info");
    expect(severityInfo("unknown").rank).toBe(2);
  });
});

describe("parseAlertTime", () => {
  it("treats naive backend timestamps as UTC", () => {
    expect(parseAlertTime("2026-07-09T15:00:00")).toBe(Date.parse("2026-07-09T15:00:00Z"));
  });

  it("respects explicit offsets and Z", () => {
    expect(parseAlertTime("2026-07-09T15:00:00Z")).toBe(Date.parse("2026-07-09T15:00:00Z"));
    expect(parseAlertTime("2026-07-09T15:00:00+02:00")).toBe(
      Date.parse("2026-07-09T13:00:00Z")
    );
  });

  it("returns NaN for empty input", () => {
    expect(Number.isNaN(parseAlertTime(""))).toBe(true);
    expect(Number.isNaN(parseAlertTime(null))).toBe(true);
  });
});

describe("formatDurationShort", () => {
  it("formats minutes, hours and days", () => {
    expect(formatDurationShort(30 * 1000)).toBe("under 1 min");
    expect(formatDurationShort(42 * 60000)).toBe("42 min");
    expect(formatDurationShort(63 * 60000)).toBe("1 h 03");
    expect(formatDurationShort(120 * 60000)).toBe("2 h");
    expect(formatDurationShort(28 * 3600000)).toBe("1 d 4 h");
  });

  it("returns empty for invalid input", () => {
    expect(formatDurationShort(-5)).toBe("");
    expect(formatDurationShort(NaN)).toBe("");
  });
});

describe("groupAlerts", () => {
  const alert = (overrides) => ({
    id: Math.random().toString(36),
    severity: "warning",
    firedAt: "2026-07-09T15:00:00Z",
    alertType: "metric",
    ...overrides,
  });

  it("groups alerts sharing a policyId, keeps singles flat", () => {
    const entries = groupAlerts([
      alert({ id: "a", policyId: 7, policyName: "CPU vs limit" }),
      alert({ id: "b", policyId: 7, policyName: "CPU vs limit" }),
      alert({ id: "c", policyId: 9, policyName: "Memory" }),
    ]);
    expect(entries).toHaveLength(2);
    const group = entries.find((entry) => entry.kind === "group");
    expect(group.count).toBe(2);
    expect(group.title).toBe("CPU vs limit");
    const single = entries.find((entry) => entry.kind === "single");
    expect(single.alert.id).toBe("c");
  });

  it("falls back to policyName when policyId is missing and never groups keyless alerts", () => {
    const entries = groupAlerts([
      alert({ id: "a", policyName: "OOM logs" }),
      alert({ id: "b", policyName: "OOM logs" }),
      alert({ id: "c", policyName: "" }),
      alert({ id: "d", policyName: "" }),
    ]);
    const groups = entries.filter((entry) => entry.kind === "group");
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(2);
    expect(entries.filter((entry) => entry.kind === "single")).toHaveLength(2);
  });

  it("sorts by worst severity, then oldest-fired first; group children oldest-first", () => {
    const entries = groupAlerts([
      alert({ id: "new-crit", severity: "critical", firedAt: "2026-07-09T15:30:00Z", policyId: 1 }),
      alert({ id: "old-crit", severity: "critical", firedAt: "2026-07-09T14:00:00Z", policyId: 1 }),
      alert({ id: "old-warn", severity: "warning", firedAt: "2026-07-09T10:00:00Z", policyId: 2 }),
      alert({ id: "info", severity: "info", firedAt: "2026-07-09T09:00:00Z", policyId: 3 }),
    ]);
    expect(entries[0].kind).toBe("group");
    expect(entries[0].alerts.map((a) => a.id)).toEqual(["old-crit", "new-crit"]);
    expect(entries[1].alert.id).toBe("old-warn");
    expect(entries[2].alert.id).toBe("info");
  });

  it("group worst severity is the minimum rank of its members", () => {
    const entries = groupAlerts([
      alert({ id: "a", severity: "warning", policyId: 1 }),
      alert({ id: "b", severity: "critical", policyId: 1 }),
    ]);
    expect(entries[0].worst.tone).toBe("danger");
  });
});

describe("bucketAlertHistory", () => {
  it("buckets fired timestamps into clock-aligned hours, newest last", () => {
    const { buckets, maxTotal, total } = bucketAlertHistory(
      [
        { severity: "critical", firedAt: "2026-07-09T15:10:00Z" },
        { severity: "critical", firedAt: "2026-07-09T15:20:00Z" },
        { severity: "warning", firedAt: "2026-07-09T14:05:00Z" },
        { severity: "info", firedAt: "2026-07-08T16:30:00Z" },
      ],
      { nowTs: NOW }
    );
    expect(buckets).toHaveLength(24);
    expect(buckets[23].critical).toBe(2);
    expect(buckets[22].warning).toBe(1);
    expect(buckets[0].info).toBe(1);
    expect(maxTotal).toBe(2);
    expect(total).toBe(4);
  });

  it("ignores rows outside the window or in the future", () => {
    const { total } = bucketAlertHistory(
      [
        { severity: "info", firedAt: "2026-07-07T15:00:00Z" },
        { severity: "info", firedAt: "2026-07-09T16:00:00Z" },
        { severity: "info", firedAt: "" },
      ],
      { nowTs: NOW }
    );
    expect(total).toBe(0);
  });

  it("severitySeries extracts one severity across buckets", () => {
    const { buckets } = bucketAlertHistory(
      [{ severity: "warning", firedAt: "2026-07-09T15:00:00Z" }],
      { nowTs: NOW }
    );
    const series = severitySeries(buckets, "warning");
    expect(series).toHaveLength(24);
    expect(series[23]).toBe(1);
    expect(series.reduce((a, b) => a + b, 0)).toBe(1);
  });
});

describe("resolvedStats", () => {
  it("counts resolved-in-window rows and computes the median time-to-resolve", () => {
    const stats = resolvedStats(
      [
        {
          status: "resolved",
          firedAt: "2026-07-09T14:00:00Z",
          resolvedAt: "2026-07-09T14:30:00Z",
          policyName: "CPU vs limit",
          resourceName: "gateway-api",
        },
        {
          status: "resolved",
          firedAt: "2026-07-09T10:00:00Z",
          resolvedAt: "2026-07-09T11:00:00Z",
        },
        { status: "active", firedAt: "2026-07-09T15:00:00Z" },
        {
          status: "resolved",
          firedAt: "2026-07-01T10:00:00Z",
          resolvedAt: "2026-07-01T11:00:00Z",
        },
      ],
      { nowTs: NOW }
    );
    expect(stats.count).toBe(2);
    expect(stats.medianMs).toBe(45 * 60000);
    expect(stats.lastResolved.title).toBe("CPU vs limit");
    expect(stats.lastResolved.resourceName).toBe("gateway-api");
    expect(stats.lastResolved.durationMs).toBe(30 * 60000);
  });

  it("returns nulls when nothing resolved in the window", () => {
    const stats = resolvedStats([{ status: "active", firedAt: "2026-07-09T15:00:00Z" }], {
      nowTs: NOW,
    });
    expect(stats.count).toBe(0);
    expect(stats.medianMs).toBeNull();
    expect(stats.lastResolved).toBeNull();
  });
});
