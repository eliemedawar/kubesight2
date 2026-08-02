// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useDashboardSeries } from "./useDashboardSeries.js";

/**
 * These exist to stop synthesis coming back.
 *
 * The previous implementation seeded a random walk whenever real samples were
 * scarce, which meant the charts were never empty and therefore never obviously
 * wrong. The property worth protecting is boring and absolute: the number of
 * points out is the number of real readings in, and nothing else.
 */

afterEach(cleanup);

let latest = null;

function Probe({ summary, range }) {
  latest = useDashboardSeries(summary, range);
  return null;
}

const summaryAt = (percent, { clusterId = "prod-eu", at = "2026-08-02T10:00:00Z", available = true } = {}) => ({
  clusterId,
  lastUpdated: at,
  cpuUsage: { available, percent },
  memoryUsage: { available, percent: percent + 5 },
});

function renderProbe(summary, range = "6h") {
  const utils = render(<Probe summary={summary} range={range} />);
  return {
    ...utils,
    update: (next, nextRange = range) =>
      act(() => utils.rerender(<Probe summary={next} range={nextRange} />)),
  };
}

describe("no fabricated history", () => {
  it("starts with exactly one point after one reading", () => {
    renderProbe(summaryAt(30));
    expect(latest.cpu).toEqual([30]);
    expect(latest.mem).toEqual([35]);
    expect(latest.observed).toBe(1);
  });

  it("holds one point per reading, never more", () => {
    const probe = renderProbe(summaryAt(30, { at: "2026-08-02T10:00:00Z" }));
    probe.update(summaryAt(40, { at: "2026-08-02T10:00:30Z" }));
    probe.update(summaryAt(50, { at: "2026-08-02T10:01:00Z" }));

    expect(latest.cpu).toEqual([30, 40, 50]);
    expect(latest.observed).toBe(3);
  });

  // The specific regression: a seeded walk filled the buffer to `capacity` on
  // first paint, so the chart always looked like it had history.
  it("does not pad the buffer towards its capacity", () => {
    renderProbe(summaryAt(30));
    expect(latest.cpu.length).toBeLessThan(latest.capacity);
    expect(latest.cpu.length).toBe(1);
  });

  it("reports nothing before the first summary arrives", () => {
    renderProbe(null);
    expect(latest.cpu).toEqual([]);
    expect(latest.observed).toBe(0);
  });

  it("exposes no invented series", () => {
    renderProbe(summaryAt(30));
    expect(latest.netIn).toBeUndefined();
    expect(latest.netOut).toBeUndefined();
    expect(latest.cpuBands).toBeUndefined();
    expect(latest.memLimit).toBeUndefined();
  });
});

describe("unavailable metrics", () => {
  // Recording a zero would be indistinguishable from a genuinely idle cluster,
  // which is the more dangerous reading of the two.
  it("records no point when the source did not answer", () => {
    const probe = renderProbe(summaryAt(30));
    probe.update({
      clusterId: "prod-eu",
      lastUpdated: "2026-08-02T10:00:30Z",
      cpuUsage: { available: false, percent: 0 },
      memoryUsage: { available: false, percent: 0 },
    });

    expect(latest.cpu).toEqual([30]);
    expect(latest.cpuAvailable).toBe(false);
  });

  it("resumes appending when the source recovers", () => {
    const probe = renderProbe(summaryAt(30));
    probe.update({
      clusterId: "prod-eu",
      lastUpdated: "2026-08-02T10:00:30Z",
      cpuUsage: { available: false, percent: 0 },
      memoryUsage: { available: false, percent: 0 },
    });
    probe.update(summaryAt(55, { at: "2026-08-02T10:01:00Z" }));

    expect(latest.cpu).toEqual([30, 55]);
    expect(latest.cpuAvailable).toBe(true);
  });
});

describe("cluster scope", () => {
  it("discards history when the cluster changes", () => {
    const probe = renderProbe(summaryAt(30));
    probe.update(summaryAt(40, { at: "2026-08-02T10:00:30Z" }));
    expect(latest.cpu).toHaveLength(2);

    probe.update(summaryAt(70, { clusterId: "prod-us", at: "2026-08-02T10:01:00Z" }));
    expect(latest.cpu).toEqual([70]);
  });

  it("keeps history when only the range changes", () => {
    const probe = renderProbe(summaryAt(30), "6h");
    probe.update(summaryAt(40, { at: "2026-08-02T10:00:30Z" }), "6h");
    probe.update(summaryAt(50, { at: "2026-08-02T10:01:00Z" }), "1h");

    expect(latest.cpu).toEqual([30, 40, 50]);
  });
});
