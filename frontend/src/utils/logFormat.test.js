import { describe, expect, it } from "vitest";
import {
  appendLogLines,
  applyLogSnapshot,
  buildIncrementalSinceTime,
  extractLogTimestamp,
  filterHealthProbeLogLines,
  filterLiveLogNoise,
  formatLogLineToLocalTime,
  formatLogLinesToLocalTime,
  getIncrementalTailCursor,
  getNewestLogTimestamp,
  isHealthProbeLogLine,
  sortLogLinesByTimestamp,
} from "./logFormat.js";

describe("logFormat", () => {
  it("formats RFC3339 timestamps to a fixed local display shape", () => {
    const formatted = formatLogLineToLocalTime("2025-06-08T12:34:56.789Z INFO message");
    expect(formatted).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} INFO message$/);
    expect(formatted).not.toContain("T");
    expect(formatted).not.toContain("Z");
  });

  it("formats kubectl space-separated timestamps", () => {
    const formatted = formatLogLineToLocalTime("2025-06-08 12:34:56.789Z INFO message");
    expect(formatted).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} INFO message$/);
  });

  it("uses the same shape for every formatted line", () => {
    const lines = formatLogLinesToLocalTime([
      "2025-06-08T12:34:56Z INFO one",
      "2025-06-08T12:34:57.100Z WARN two",
    ]);
    lines.forEach((line) => {
      expect(line).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} /);
    });
  });

  it("preserves the raw timestamp token for incremental polling", () => {
    expect(extractLogTimestamp("2025-06-08T12:34:56.789Z INFO message")).toBe(
      "2025-06-08T12:34:56.789Z"
    );
  });

  it("removes duplicate werkzeug timestamps when kubectl already timestamps the line", () => {
    const formatted = formatLogLineToLocalTime(
      '2026-06-08T11:04:32.059Z 10.244.0.1 - - [08/Jun/2026 11:04:32] "GET /health HTTP/1.1" 200 -'
    );
    expect(formatted).not.toMatch(/\[08\/Jun\/2026/);
    expect(formatted).toContain('10.244.0.1 - - "GET /health HTTP/1.1" 200 -');
    expect(formatted).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} /);
  });

  it("sorts log lines chronologically", () => {
    const sorted = sortLogLinesByTimestamp([
      "2025-06-08T12:34:58Z INFO three",
      "2025-06-08T12:34:56Z INFO one",
      "2025-06-08T12:34:57Z INFO two",
    ]);
    expect(sorted.map((line) => line.split(" ")[2])).toEqual(["one", "two", "three"]);
  });

  it("filters health probe access-log noise from live snapshots", () => {
    expect(
      isHealthProbeLogLine('2026-06-08T11:04:32Z 10.244.0.1 - - "GET /health HTTP/1.1" 200 -')
    ).toBe(true);
    expect(filterHealthProbeLogLines([
      '2026-06-08T11:04:32Z 10.244.0.1 - - "GET /health HTTP/1.1" 200 -',
      "2026-06-08T11:04:33Z INFO payment processed",
    ])).toEqual(["2026-06-08T11:04:33Z INFO payment processed"]);
  });

  it("falls back to probe-only lines instead of an empty snapshot", () => {
    const probeOnly = ['2026-06-08T11:04:32Z 10.244.0.1 - - "GET /health HTTP/1.1" 200 -'];
    expect(applyLogSnapshot(probeOnly)).toEqual(probeOnly);
  });

  it("drops self-referential log polling from live snapshots", () => {
    const noisy = [
      "2026-06-08T16:08:31.123Z GET /api/clusters/docker-desktop/namespaces/kubesight/pods/backend/containers/backend/logs 200 5ms",
      "2026-06-08T16:08:32.123Z INFO deployment started",
    ];
    expect(filterLiveLogNoise(noisy)).toEqual(["2026-06-08T16:08:32.123Z INFO deployment started"]);
    expect(
      applyLogSnapshot([
        '2026-06-08T16:08:31.123Z 127.0.0.1 - - "GET /api/logs?live=true HTTP/1.1" 200 -',
      ])
    ).toEqual([]);
  });

  it("builds a sorted snapshot from kubectl output", () => {
    const snapshot = applyLogSnapshot([
      "2025-06-08T12:34:58Z INFO three",
      "2025-06-08T12:34:56Z INFO one",
      "2025-06-08T12:34:57Z INFO two",
    ]);
    expect(snapshot?.map((line) => line.split(" ")[2])).toEqual(["one", "two", "three"]);
  });

  it("returns the newest timestamp token from the buffer", () => {
    expect(
      getNewestLogTimestamp([
        "2025-06-08T12:34:56Z INFO one",
        "2025-06-08T12:34:59.123Z INFO two",
      ])
    ).toBe("2025-06-08T12:34:59.123Z");
  });

  it("parses kubectl space-separated timestamps for tail cursors", () => {
    expect(
      getNewestLogTimestamp(["2025-06-08 12:34:59.123Z INFO two"])
    ).toBe("2025-06-08T12:34:59.123Z");
  });

  it("advances the incremental tail cursor by 1ms", () => {
    expect(buildIncrementalSinceTime("2025-06-08T12:34:59.123Z")).toBe(
      "2025-06-08T12:34:59.124Z"
    );
    expect(
      getIncrementalTailCursor(["2025-06-08T12:34:59.123Z INFO tail"])
    ).toBe("2025-06-08T12:34:59.124Z");
  });

  it("appends only new log lines without duplicating existing buffer", () => {
    const existing = [
      "2025-06-08T12:34:56Z INFO one",
      "2025-06-08T12:34:57Z INFO two",
    ];
    const merged = appendLogLines(existing, [
      "2025-06-08T12:34:57Z INFO two",
      "2025-06-08T12:34:58Z INFO three",
    ]);
    expect(merged).toHaveLength(3);
    expect(merged.map((line) => line.split(" ")[2])).toEqual(["one", "two", "three"]);
  });
});
