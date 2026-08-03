import { describe, expect, it } from "vitest";
import { timeAgo as mobileAppsTimeAgo } from "../components/mobileApps/common.jsx";
import { freshness, relativeTime } from "./relativeTime.js";

const NOW = Date.parse("2026-08-02T12:00:00Z");
const ago = (ms) => new Date(NOW - ms).toISOString();

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe("relativeTime", () => {
  it("reports recent times as just now", () => {
    expect(relativeTime(ago(0), NOW)).toBe("just now");
    expect(relativeTime(ago(59_000), NOW)).toBe("just now");
  });

  it("steps through minutes, hours and days", () => {
    expect(relativeTime(ago(5 * MINUTE), NOW)).toBe("5m ago");
    expect(relativeTime(ago(3 * HOUR), NOW)).toBe("3h ago");
    expect(relativeTime(ago(2 * DAY), NOW)).toBe("2d ago");
  });

  it("falls back to a date beyond a month", () => {
    expect(relativeTime(ago(60 * DAY), NOW)).toMatch(/\d/);
    expect(relativeTime(ago(60 * DAY), NOW)).not.toMatch(/ago/);
  });

  // The bug that makes this worth centralising: the backend serialises UTC with
  // isoformat() and can omit the suffix. Read as local time, every duration
  // shifts by the viewer's offset -- on a freshness indicator that means
  // confidently reporting stale data as current.
  it("treats a naive backend timestamp as UTC, not local", () => {
    const naive = "2026-08-02T11:30:00";
    expect(relativeTime(naive, NOW)).toBe("30m ago");
  });

  it("returns nothing for a missing or unparseable value", () => {
    expect(relativeTime(null, NOW)).toBe("");
    expect(relativeTime("", NOW)).toBe("");
    expect(relativeTime("not a date", NOW)).toBe("");
  });

  // Small negative deltas are clock skew, not time travel. "in 4 seconds" on a
  // row that was just written reads as a bug.
  it("absorbs small clock skew instead of reporting the future", () => {
    expect(relativeTime(new Date(NOW + 5_000).toISOString(), NOW)).toBe("just now");
  });

  it("still names a genuinely future timestamp", () => {
    expect(relativeTime(new Date(NOW + 2 * HOUR).toISOString(), NOW)).toBe("in the future");
  });
});

describe("compact style", () => {
  // Dense table columns: "5m" carries the same meaning as "5m ago" in a third
  // of the width.
  it("drops the suffix", () => {
    expect(relativeTime(ago(5 * MINUTE), { now: NOW, style: "compact" })).toBe("5m");
    expect(relativeTime(ago(3 * HOUR), { now: NOW, style: "compact" })).toBe("3h");
    expect(relativeTime(ago(2 * DAY), { now: NOW, style: "compact" })).toBe("2d");
  });

  it("shortens the recent and future cases too", () => {
    expect(relativeTime(ago(0), { now: NOW, style: "compact" })).toBe("now");
    expect(
      relativeTime(new Date(NOW + 2 * HOUR).toISOString(), { now: NOW, style: "compact" })
    ).toBe("soon");
  });

  it("renders a caller-chosen placeholder for a missing value", () => {
    expect(relativeTime(null, { now: NOW, empty: "—" })).toBe("—");
    expect(relativeTime(null, { now: NOW })).toBe("");
  });

  // Callers written before the options object passed `now` positionally.
  it("still accepts a bare timestamp as the second argument", () => {
    expect(relativeTime(ago(5 * MINUTE), NOW)).toBe("5m ago");
  });
});

describe("the copies that converged onto this", () => {
  it("keeps the Mobile Apps wrapper connected to the shared formatter", () => {
    expect(mobileAppsTimeAgo(null)).toBe("");
  });

  // Three of the four hand-rolled implementations used `new Date(iso)`, which
  // reads a naive backend timestamp as local time and shifts every age by the
  // viewer's offset. On a "last synced" label that reports stale data as
  // current.
  it("agrees with the naive-UTC reading the old copies got wrong", () => {
    const naive = "2026-08-02T11:30:00";
    expect(relativeTime(naive, { now: NOW })).toBe("30m ago");
    expect(relativeTime(naive, { now: NOW, style: "compact" })).toBe("30m");

    // What the old implementations did, for contrast: parsed as local.
    const localReading = Math.round((NOW - new Date(naive).getTime()) / 60000);
    const utcReading = 30;
    const offsetMinutes = new Date().getTimezoneOffset();
    expect(localReading - utcReading).toBe(-offsetMinutes);
  });
});

describe("freshness", () => {
  it("is fresh inside the staleness window and stale outside it", () => {
    expect(freshness(ago(MINUTE), { staleAfterMs: 5 * MINUTE, now: NOW }).state).toBe("fresh");
    expect(freshness(ago(10 * MINUTE), { staleAfterMs: 5 * MINUTE, now: NOW }).state).toBe("stale");
  });

  // A never-fetched value is not a fresh one. Collapsing the two would show a
  // green indicator for data that has never arrived.
  it("reports unknown for a missing timestamp rather than fresh", () => {
    const result = freshness(null, { staleAfterMs: MINUTE, now: NOW });
    expect(result.state).toBe("unknown");
    expect(result.label).toBe("Never");
    expect(result.ageMs).toBeNull();
  });

  it("is fresh when no staleness window is given", () => {
    expect(freshness(ago(365 * DAY), { now: NOW }).state).toBe("fresh");
  });

  it("reports age", () => {
    expect(freshness(ago(3 * MINUTE), { staleAfterMs: HOUR, now: NOW }).ageMs).toBe(3 * MINUTE);
  });
});
