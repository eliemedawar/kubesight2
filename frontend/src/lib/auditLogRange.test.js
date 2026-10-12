import { describe, expect, it } from "vitest";
import { describeRange, localDay, rangeBounds } from "./auditLogRange";

const NOW = Date.parse("2026-10-12T15:00:00Z");

describe("rangeBounds", () => {
  it("leaves 'any time' open on both sides", () => {
    expect(rangeBounds("all", {}, NOW)).toEqual({});
  });

  it("counts quick ranges back from now, open-ended", () => {
    expect(rangeBounds("24h", {}, NOW)).toEqual({ from: "2026-10-11T15:00:00.000Z" });
    expect(rangeBounds("7d", {}, NOW)).toEqual({ from: "2026-10-05T15:00:00.000Z" });
    expect(rangeBounds("30d", {}, NOW)).toEqual({ from: "2026-09-12T15:00:00.000Z" });
  });

  it("sends a custom range as local whole days, the end exclusive", () => {
    const { from, to } = rangeBounds("custom", { fromDay: "2026-10-01", toDay: "2026-10-03" });
    expect(from).toBe(new Date(2026, 9, 1).toISOString());
    expect(to).toBe(new Date(2026, 9, 4).toISOString());
  });

  it("allows one open side and the same day twice", () => {
    expect(rangeBounds("custom", { fromDay: "2026-10-01" })).toEqual({
      from: new Date(2026, 9, 1).toISOString(),
    });
    expect(rangeBounds("custom", { toDay: "2026-10-01" })).toEqual({
      to: new Date(2026, 9, 2).toISOString(),
    });
    const same = rangeBounds("custom", { fromDay: "2026-10-01", toDay: "2026-10-01" });
    expect(same.error).toBeUndefined();
  });

  it("refuses an end before the start and dates that are not dates", () => {
    expect(rangeBounds("custom", { fromDay: "2026-10-05", toDay: "2026-10-01" }).error).toBeTruthy();
    expect(rangeBounds("custom", { fromDay: "yesterday" }).error).toBeTruthy();
  });
});

describe("describeRange", () => {
  it("names the window", () => {
    expect(describeRange("all")).toBe("");
    expect(describeRange("7d")).toBe("in the last 7 days");
    expect(describeRange("custom", { fromDay: "2026-10-01", toDay: "2026-10-03" })).toBe(
      "from 2026-10-01 to 2026-10-03",
    );
    expect(describeRange("custom", { fromDay: "2026-10-01", toDay: "2026-10-01" })).toBe("on 2026-10-01");
    expect(describeRange("custom", { fromDay: "2026-10-01" })).toBe("since 2026-10-01");
  });
});

describe("localDay", () => {
  it("formats a local calendar day", () => {
    expect(localDay(new Date(2026, 0, 5, 23, 30))).toBe("2026-01-05");
  });
});
