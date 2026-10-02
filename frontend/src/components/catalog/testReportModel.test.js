import { describe, expect, it } from "vitest";
import {
  REPORT_PRESETS,
  addPreset,
  caseTitle,
  describeMetric,
  findPreset,
  formatPct,
  formatTestDuration,
  lastDelta,
  sparkline,
  testBadgeParts,
  testBadgeTitle,
} from "./testReportModel.js";

const summary = (overrides = {}) => ({
  total: 133,
  passed: 128,
  failed: 2,
  errors: 1,
  skipped: 2,
  linesPct: 81.24,
  branchesPct: 70,
  parseErrors: 0,
  ...overrides,
});

describe("formatPct", () => {
  it("drops a trailing .0 and never rounds up to 100", () => {
    expect(formatPct(81)).toBe("81%");
    expect(formatPct(81.26)).toBe("81.2%");
    expect(formatPct(99.96)).toBe("99.9%");
    expect(formatPct(100)).toBe("100%");
    expect(formatPct(null)).toBe("");
  });
});

describe("testBadgeParts", () => {
  it("reads 128 passed · 3 failed · 81.2% cov, errors counted as failed", () => {
    const parts = testBadgeParts(summary());
    expect(parts.map((part) => part.text)).toEqual(["128 passed", "3 failed", "81.2% cov"]);
    expect(parts.find((part) => part.key === "failed").tone).toBe("bad");
  });

  it("shows passed as good when nothing failed", () => {
    const parts = testBadgeParts(summary({ failed: 0, errors: 0, passed: 131 }));
    expect(parts[0]).toMatchObject({ text: "131 passed", tone: "ok" });
    expect(parts.some((part) => part.key === "failed")).toBe(false);
  });

  it("shows coverage alone for a coverage-only build", () => {
    const parts = testBadgeParts({ total: null, linesPct: 64.5, branchesPct: null, parseErrors: 0 });
    expect(parts.map((part) => part.text)).toEqual(["64.5% cov"]);
  });

  it("says the reports were unreadable rather than nothing", () => {
    expect(testBadgeParts({ total: null, linesPct: null, parseErrors: 2 })[0].text).toBe(
      "reports unreadable"
    );
  });

  it("is empty when nothing was collected", () => {
    expect(testBadgeParts(null)).toEqual([]);
  });
});

describe("testBadgeTitle", () => {
  it("spells out every count", () => {
    expect(testBadgeTitle(summary())).toBe(
      "128 passed, 3 failed (1 error), 2 skipped of 133 tests · coverage: lines 81.2%, branches 70%"
    );
  });
});

describe("formatTestDuration", () => {
  it("covers milliseconds to hours", () => {
    expect(formatTestDuration(0.25)).toBe("250 ms");
    expect(formatTestDuration(2.345)).toBe("2.3 s");
    expect(formatTestDuration(184)).toBe("3m 04s");
    expect(formatTestDuration(3720)).toBe("1h 02m");
    expect(formatTestDuration(null)).toBe("—");
  });
});

describe("describeMetric and caseTitle", () => {
  it("describes counts and stays quiet for a bare rate", () => {
    expect(describeMetric({ covered: 162, total: 200, pct: 81 }, "lines")).toBe("162 / 200 lines");
    expect(describeMetric({ covered: null, total: null, pct: 81 }, "lines")).toBe("");
  });

  it("does not repeat the class when the name already carries it", () => {
    expect(caseTitle({ name: "refunds", classname: "com.x.PaymentTest" })).toEqual({
      name: "refunds",
      owner: "com.x.PaymentTest",
    });
    expect(caseTitle({ name: "Cart totals", classname: "Cart totals" }).owner).toBe("");
  });
});

describe("sparkline", () => {
  it("leaves a gap for a missing reading instead of drawing zero", () => {
    const line = sparkline([80, null, 82, 83], { width: 100, height: 20, pad: 0 });
    expect(line.segments).toHaveLength(2);
    expect(line.points[1]).toBeNull();
  });

  it("keeps a nearly flat series nearly flat", () => {
    const line = sparkline([81, 81.1], { width: 100, height: 20, pad: 0, minSpan: 10 });
    const [a, b] = line.points;
    expect(Math.abs(a.y - b.y)).toBeLessThan(1);
  });

  it("anchors counts at a zero floor", () => {
    const line = sparkline([0, 4], { width: 100, height: 20, pad: 0, floor: 0 });
    expect(line.points[0].y).toBe(20);
    expect(line.points[1].y).toBe(0);
  });

  it("handles a single point and no points", () => {
    expect(sparkline([5]).points[0].x).toBe(60);
    expect(sparkline([null]).segments).toEqual([]);
  });

  it("reports the last change", () => {
    expect(lastDelta([70, null, 72.5])).toBe(2.5);
    expect(lastDelta([70])).toBeNull();
  });
});

describe("report presets", () => {
  it("are typed as the two report kinds", () => {
    const items = REPORT_PRESETS.flatMap((group) => group.items);
    expect(new Set(items.map((item) => item.type))).toEqual(new Set(["test-report", "coverage-report"]));
    expect(findPreset("surefire").path).toBe("**/target/surefire-reports/TEST-*.xml");
  });

  it("fill a blank row, re-type an existing path and never duplicate", () => {
    const preset = findPreset("lcov");
    expect(addPreset([{ path: "", type: "" }], preset)).toEqual([
      { path: "coverage/lcov.info", type: "coverage-report" },
    ]);
    expect(addPreset([{ path: "coverage/lcov.info", type: "binary" }], preset)).toEqual([
      { path: "coverage/lcov.info", type: "coverage-report" },
    ]);
    expect(addPreset([{ path: "target/*.jar", type: "jar" }], preset)).toHaveLength(2);
  });
});
