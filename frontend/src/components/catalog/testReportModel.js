/**
 * Formatting for a build's test results and coverage.
 *
 * Pure functions, shared by the build table, the stage matrix gutter, the
 * drawer's Tests section and the trend strip — so "3 failed" means the same
 * number in all four places. A test that errored (it could not run: setup
 * threw, a connection was refused) is counted as failed everywhere a single
 * number is shown, and split out only where there is room to say "1 error".
 */

/** 81.2 -> "81.2%", 81 -> "81%", null -> "". Never "100%" for 99.96. */
export function formatPct(value) {
  if (value == null || Number.isNaN(Number(value))) return "";
  const number = Math.max(0, Math.min(100, Number(value)));
  // Floored to one decimal: rounding 99.96 up would claim full coverage.
  const floored = Math.floor(number * 10) / 10;
  return `${Number.isInteger(floored) ? floored.toFixed(0) : floored.toFixed(1)}%`;
}

export const failedCount = (summary) =>
  summary ? (summary.failed || 0) + (summary.errors || 0) : 0;

export const hasTests = (summary) => Boolean(summary) && summary.total != null;

export const hasCoverage = (summary) =>
  Boolean(summary) && (summary.linesPct != null || summary.branchesPct != null);

/**
 * The compact badge: "128 passed · 3 failed · 81% cov".
 *
 * Returns parts rather than a string so each can carry its own tone; the
 * caller joins them. Empty when nothing was collected — a build without
 * reports shows nothing, not "0 passed", which would read as a test run.
 */
export function testBadgeParts(summary) {
  if (!summary) return [];
  const parts = [];
  if (hasTests(summary)) {
    const failed = failedCount(summary);
    if (summary.total === 0) {
      parts.push({ key: "none", text: "0 tests", tone: "muted" });
    } else {
      parts.push({ key: "passed", text: `${summary.passed ?? 0} passed`, tone: failed ? "muted" : "ok" });
      if (failed) parts.push({ key: "failed", text: `${failed} failed`, tone: "bad" });
    }
  }
  if (summary.linesPct != null) {
    parts.push({ key: "cov", text: `${formatPct(summary.linesPct)} cov`, tone: "muted" });
  } else if (summary.branchesPct != null) {
    parts.push({ key: "cov", text: `${formatPct(summary.branchesPct)} branch cov`, tone: "muted" });
  }
  if (!parts.length && summary.parseErrors) {
    parts.push({ key: "unreadable", text: "reports unreadable", tone: "warn" });
  }
  return parts;
}

/** The long form for a tooltip or a screen reader. */
export function testBadgeTitle(summary) {
  if (!summary) return "";
  const pieces = [];
  if (hasTests(summary)) {
    const detail = [];
    if (summary.errors) detail.push(`${summary.errors} error${summary.errors === 1 ? "" : "s"}`);
    pieces.push(
      `${summary.passed ?? 0} passed, ${failedCount(summary)} failed${
        detail.length ? ` (${detail.join(", ")})` : ""
      }, ${summary.skipped ?? 0} skipped of ${summary.total} test${summary.total === 1 ? "" : "s"}`
    );
  }
  const coverage = [];
  if (summary.linesPct != null) coverage.push(`lines ${formatPct(summary.linesPct)}`);
  if (summary.branchesPct != null) coverage.push(`branches ${formatPct(summary.branchesPct)}`);
  if (coverage.length) pieces.push(`coverage: ${coverage.join(", ")}`);
  if (summary.parseErrors) {
    pieces.push(
      `${summary.parseErrors} report file${summary.parseErrors === 1 ? "" : "s"} could not be read`
    );
  }
  return pieces.join(" · ");
}

/** "1.2 s", "3m 04s", "850 ms" — test time is often well under a second. */
export function formatTestDuration(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return "—";
  const value = Math.max(0, Number(seconds));
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(value / 60);
  const rest = Math.round(value % 60);
  if (minutes < 60) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** "162 / 200 lines" from a {covered,total} metric; "" when only a rate exists. */
export function describeMetric(metric, unit) {
  if (!metric || metric.covered == null || metric.total == null) return "";
  return `${metric.covered.toLocaleString()} / ${metric.total.toLocaleString()} ${unit}`;
}

/** The headline a failed case is listed under: class + name, without repeating. */
export function caseTitle(failure) {
  const name = failure?.name || "(unnamed test)";
  const owner = failure?.classname || failure?.suite || "";
  if (!owner || name.startsWith(owner) || owner === name) return { name, owner: "" };
  return { name, owner };
}

/**
 * Points for a sparkline, x evenly spaced, y scaled into [pad, height - pad].
 *
 * `null` values are gaps, never zeros: a build whose coverage file was not
 * readable did not have 0% coverage. Each run of consecutive values becomes
 * its own path segment so a gap is visibly a gap.
 *
 * `minSpan` keeps a flat series flat: without it a coverage figure moving
 * from 81.0% to 81.1% fills the whole height and looks like a cliff.
 */
export function sparkline(
  values,
  { width = 120, height = 28, pad = 3, padX = null, minSpan = 0, floor = null } = {}
) {
  const xPad = padX == null ? pad : padX;
  const numbers = values.filter((value) => value != null && !Number.isNaN(Number(value))).map(Number);
  if (!numbers.length) return { segments: [], points: [], min: null, max: null };
  let min = floor != null ? Math.min(floor, ...numbers) : Math.min(...numbers);
  let max = Math.max(...numbers);
  if (max - min < minSpan) {
    const mid = (max + min) / 2;
    min = mid - minSpan / 2;
    max = mid + minSpan / 2;
    if (floor != null && min < floor) {
      max += floor - min;
      min = floor;
    }
  }
  const span = max - min || 1;
  const step = values.length > 1 ? (width - xPad * 2) / (values.length - 1) : 0;
  const points = values.map((value, index) => {
    if (value == null || Number.isNaN(Number(value))) return null;
    const x = values.length > 1 ? xPad + step * index : width / 2;
    const y = height - pad - ((Number(value) - min) / span) * (height - pad * 2);
    return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10, value: Number(value), index };
  });
  const segments = [];
  let current = [];
  for (const point of points) {
    if (point) current.push(point);
    else if (current.length) {
      segments.push(current);
      current = [];
    }
  }
  if (current.length) segments.push(current);
  return {
    segments: segments.map((segment) =>
      segment.map((point, index) => `${index ? "L" : "M"}${point.x} ${point.y}`).join(" ")
    ),
    points,
    min,
    max,
  };
}

/** Change between the last two readings that exist, for "▲ 1.2" next to a trend. */
export function lastDelta(values) {
  const present = values.filter((value) => value != null && !Number.isNaN(Number(value)));
  if (present.length < 2) return null;
  return Number(present[present.length - 1]) - Number(present[present.length - 2]);
}

/**
 * Presets for the stage's "Files to keep" editor. Paths are globs relative to
 * the stage's working directory, matched with Python's recursive glob on the
 * Kubernetes runner and the agents alike: `**` spans any depth INCLUDING none,
 * so `**\/target/surefire-reports` finds a single-module build's reports as
 * well as every module's in a multi-module one. Hidden directories are never
 * walked.
 */
export const REPORT_PRESETS = [
  {
    group: "Test results",
    items: [
      { id: "surefire", label: "Maven Surefire (unit tests)", path: "**/target/surefire-reports/TEST-*.xml", type: "test-report" },
      { id: "failsafe", label: "Maven Failsafe (integration tests)", path: "**/target/failsafe-reports/TEST-*.xml", type: "test-report" },
      { id: "gradle", label: "Gradle", path: "**/build/test-results/**/*.xml", type: "test-report" },
      { id: "junit", label: "jest-junit / generic junit.xml", path: "junit.xml", type: "test-report" },
      { id: "pytest", label: "pytest --junitxml=report.xml", path: "report.xml", type: "test-report" },
      { id: "trx", label: ".NET TRX (dotnet test --logger trx)", path: "**/TestResults/*.trx", type: "test-report" },
    ],
  },
  {
    group: "Coverage",
    items: [
      { id: "jacoco", label: "JaCoCo XML", path: "**/jacoco*.xml", type: "coverage-report" },
      { id: "cobertura-js", label: "Cobertura (Istanbul / jest)", path: "**/cobertura-coverage.xml", type: "coverage-report" },
      { id: "coverage-py", label: "Cobertura (coverage.py: coverage.xml)", path: "coverage.xml", type: "coverage-report" },
      { id: "coverlet", label: "Cobertura (.NET coverlet)", path: "**/coverage.cobertura.xml", type: "coverage-report" },
      { id: "lcov", label: "lcov", path: "coverage/lcov.info", type: "coverage-report" },
      { id: "istanbul", label: "Istanbul json-summary", path: "coverage/coverage-summary.json", type: "coverage-report" },
    ],
  },
];

export const findPreset = (id) =>
  REPORT_PRESETS.flatMap((group) => group.items).find((item) => item.id === id) || null;

/** Rows with the preset appended, unless that exact path is already kept. */
export function addPreset(rows, preset) {
  if (!preset) return rows;
  const exists = (rows || []).some((row) => String(row.path || "").trim() === preset.path);
  if (exists) {
    return (rows || []).map((row) =>
      String(row.path || "").trim() === preset.path ? { ...row, type: preset.type } : row
    );
  }
  // A blank row left by "Add file" is filled rather than left dangling above it.
  const blank = (rows || []).findIndex((row) => !String(row.path || "").trim());
  if (blank >= 0) {
    return rows.map((row, index) => (index === blank ? { ...row, path: preset.path, type: preset.type } : row));
  }
  return [...(rows || []), { path: preset.path, type: preset.type }];
}
