import { useEffect, useMemo, useState } from "react";
import { getCiBuildTests, getCiServiceTestTrend } from "../../api/ciApi.js";
import { parseApiTime } from "../../lib/apiTime.js";
import { isBuildActive } from "./ciShared.jsx";
import {
  caseTitle,
  describeMetric,
  failedCount,
  formatPct,
  formatTestDuration,
  hasCoverage,
  hasTests,
  sparkline,
  testBadgeParts,
  testBadgeTitle,
} from "./testReportModel.js";

// The Kubernetes collector uploads after the last stage, and after a FAILED
// stage that upload can land a little after the build is marked failed. The
// drawer keeps asking for that long before it settles on "nothing collected".
const LATE_UPLOAD_WINDOW_MS = 3 * 60 * 1000;
const LATE_UPLOAD_POLL_MS = 5000;

/**
 * "128 passed · 3 failed · 81% cov" — the one-line verdict a build list shows.
 * Renders nothing for a build that kept no reports.
 */
export function TestBadge({ summary, className = "" }) {
  const parts = testBadgeParts(summary);
  if (!parts.length) return null;
  const title = testBadgeTitle(summary);
  return (
    <span className={`sg-ci-tbadge ${className}`} title={title} aria-label={`Tests: ${title}`}>
      {parts.map((part, index) => (
        <span key={part.key} className={`sg-ci-tbadge-part is-${part.tone}`}>
          {index > 0 && <i aria-hidden="true">·</i>}
          {part.text}
        </span>
      ))}
    </span>
  );
}

/**
 * The drawer's Tests section: tiles, coverage bars, and the failed cases.
 *
 * Fetched once the build has finished (results do not change after that), and
 * again whenever the build's compact summary moves — an agent build uploads
 * reports stage by stage while the drawer is open.
 */
export default function TestResultsPanel({ build }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [lateTick, setLateTick] = useState(0);
  const active = isBuildActive(build.status);
  const summaryKey = JSON.stringify(build.testSummary || null);

  useEffect(() => {
    let cancelled = false;
    if (active && summaryKey === "null") {
      setData(null);
      return undefined;
    }
    getCiBuildTests(build.id)
      .then((payload) => {
        if (!cancelled) {
          setData(payload);
          setError("");
        }
      })
      .catch((err) => !cancelled && setError(err.message || "Could not load the test results."));
    return () => {
      cancelled = true;
    };
  }, [build.id, active, summaryKey, lateTick]);

  // A just-finished build with nothing yet may still be uploading.
  useEffect(() => {
    if (active || !data || data.summary) return undefined;
    const finished = parseApiTime(build.finishedAt);
    if (Number.isNaN(finished) || Date.now() - finished > LATE_UPLOAD_WINDOW_MS) return undefined;
    if (!data.declared?.length) return undefined;
    const id = window.setTimeout(() => setLateTick((tick) => tick + 1), LATE_UPLOAD_POLL_MS);
    return () => window.clearTimeout(id);
  }, [active, data, build.finishedAt]);

  if (error) {
    return (
      <section className="sg-ci-tests" aria-label="Test results">
        <p className="form-label">Tests</p>
        <p className="banner-message error">{error}</p>
      </section>
    );
  }
  if (!data) {
    return active ? (
      <section className="sg-ci-tests is-quiet" aria-label="Test results">
        <p className="form-label">Tests</p>
        <p className="muted sg-ci-tests-empty">
          Results appear when the stage that writes the test reports finishes.
        </p>
      </section>
    ) : null;
  }
  if (!data.summary) return <TestsEmpty declared={data.declared || []} active={active} />;
  return <TestsDetail data={data} />;
}

function TestsEmpty({ declared, active }) {
  return (
    <section className="sg-ci-tests is-quiet" aria-label="Test results">
      <p className="form-label">Tests</p>
      {declared.length === 0 ? (
        <p className="muted sg-ci-tests-empty">
          No test reports were collected. Declare them in the stage&apos;s <b>Files to keep</b> as{" "}
          <code>test-report</code> or <code>coverage-report</code> — there are presets for Maven,
          Gradle, jest, pytest, .NET, JaCoCo, Cobertura and lcov.
        </p>
      ) : (
        <p className="muted sg-ci-tests-empty">
          {active ? "No report has arrived yet. " : "No report file matched. "}
          This build keeps{" "}
          {declared.slice(0, 3).map((item, index) => (
            <span key={`${item.stage}-${item.path}`}>
              {index > 0 && ", "}
              <code>{item.path}</code>
              {item.stage ? ` (${item.stage})` : ""}
            </span>
          ))}
          {declared.length > 3 ? ` and ${declared.length - 3} more` : ""}
          {active
            ? "."
            : ". Paths are relative to the stage's working directory; check the tests ran and wrote their reports there."}
        </p>
      )}
    </section>
  );
}

function Tile({ label, value, tone, sub }) {
  return (
    <div className={`sg-ci-tests-tile${tone ? ` is-${tone}` : ""}`}>
      <span className="sg-ci-tests-tile-label">{label}</span>
      <span className="sg-ci-tests-tile-value">{value}</span>
      {sub && <span className="sg-ci-tests-tile-sub">{sub}</span>}
    </div>
  );
}

function CoverageBar({ label, metric, unit }) {
  if (!metric || metric.pct == null) return null;
  const counts = describeMetric(metric, unit);
  return (
    <div className="sg-ci-cov-row">
      <span className="sg-ci-cov-label">{label}</span>
      <span
        className="sg-ci-cov-bar"
        role="meter"
        aria-label={`${label} coverage`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={metric.pct}
        aria-valuetext={formatPct(metric.pct)}
      >
        <i style={{ width: `${Math.max(0, Math.min(100, metric.pct))}%` }} />
      </span>
      <span className="sg-ci-cov-pct">{formatPct(metric.pct)}</span>
      <span className="sg-ci-cov-counts">{counts}</span>
    </div>
  );
}

function TestsDetail({ data }) {
  const { summary, totals, coverage } = data;
  const failed = failedCount(summary);
  const failures = data.failures || [];
  const errors = data.errors || [];
  const fileCount = summary.reportCount;
  return (
    <section className="sg-ci-tests" aria-label="Test results">
      <div className="sg-ci-tests-head">
        <p className="form-label">Tests</p>
        <span className="muted">
          from {fileCount} report file{fileCount === 1 ? "" : "s"}
        </span>
      </div>

      {hasTests(summary) && (
        <div className="sg-ci-tests-tiles">
          <Tile label="Passed" value={summary.passed} tone={failed ? null : "ok"} />
          <Tile
            label="Failed"
            value={failed}
            tone={failed ? "bad" : null}
            sub={summary.errors ? `${summary.errors} error${summary.errors === 1 ? "" : "s"}` : null}
          />
          <Tile label="Skipped" value={summary.skipped} />
          <Tile
            label="Total"
            value={summary.total}
            sub={summary.flaky ? `${summary.flaky} flaky` : null}
          />
          <Tile label="Test time" value={formatTestDuration(totals?.durationSeconds)} />
        </div>
      )}

      {hasCoverage(summary) && coverage && (
        <div className="sg-ci-cov">
          <CoverageBar label="Lines" metric={coverage.lines} unit="lines" />
          <CoverageBar label="Branches" metric={coverage.branches} unit="branches" />
          {coverage.note && <p className="field-hint">{coverage.note}</p>}
        </div>
      )}

      {errors.length > 0 && (
        <div className="sg-ci-tests-errors" role="note">
          <strong>
            {data.errorCount || errors.length} report file
            {(data.errorCount || errors.length) === 1 ? "" : "s"} could not be read
          </strong>
          <ul>
            {errors.slice(0, 5).map((item, index) => (
              <li key={`${item.artifactId}-${index}`}>
                <code title={item.name}>{item.name}</code> — {item.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {failures.length > 0 && (
        <div className="sg-ci-tests-fails-wrap">
          <p className="sg-ci-tests-subhead">
            Failed tests
            {data.failuresTruncated ? (
              <span className="muted"> · showing {failures.length} of {data.failureCount}</span>
            ) : null}
          </p>
          <ul className="sg-ci-tests-fails">
            {failures.map((failure, index) => (
              <FailedCase key={`${failure.artifactId}-${index}`} failure={failure} />
            ))}
          </ul>
        </div>
      )}

      {(data.reports || []).length > 0 && (
        <details className="sg-ci-tests-files">
          <summary>Report files ({data.reports.length})</summary>
          <ul>
            {data.reports.map((report, index) => (
              <li key={`${report.artifactId}-${index}`}>
                <code title={report.name}>{report.name}</code>
                {report.stage && <span className="muted">{report.stage}</span>}
                <span className="sg-ci-tests-file-count">
                  {report.kind === "tests"
                    ? `${report.tests} test${report.tests === 1 ? "" : "s"}${
                        report.failed ? `, ${report.failed} failed` : ""
                      }`
                    : report.lines?.pct != null
                    ? `${formatPct(report.lines.pct)} lines`
                    : report.branches?.pct != null
                    ? `${formatPct(report.branches.pct)} branches`
                    : "coverage"}
                </span>
              </li>
            ))}
          </ul>
          {data.reportsTruncated && (
            <p className="field-hint">
              Only the first {data.reports.length} files are listed; the totals count every one.
            </p>
          )}
        </details>
      )}
    </section>
  );
}

function FailedCase({ failure }) {
  const { name, owner } = caseTitle(failure);
  const where = [owner, failure.file].filter(Boolean);
  const body = failure.details || failure.message;
  return (
    <li className={`sg-ci-tcase is-${failure.kind === "error" ? "error" : "failed"}`}>
      <details>
        <summary>
          <span className="sg-ci-tcase-mark" aria-hidden="true" />
          <span className="sg-ci-tcase-main">
            <span className="sg-ci-tcase-name">{name}</span>
            {failure.kind === "error" && <span className="sg-ci-tcase-kind">error</span>}
            {where.length > 0 && <span className="sg-ci-tcase-where">{where.join(" · ")}</span>}
            {failure.message && <span className="sg-ci-tcase-msg">{failure.message}</span>}
          </span>
        </summary>
        <div className="sg-ci-tcase-body">
          {failure.suite && failure.suite !== owner && (
            <p className="field-hint">Suite: {failure.suite}</p>
          )}
          {failure.type && <p className="field-hint">Type: {failure.type}</p>}
          {body ? <pre>{body}</pre> : <p className="muted">The report gave no message for this failure.</p>}
          {failure.report && <p className="field-hint">Report: {failure.report}</p>}
        </div>
      </details>
    </li>
  );
}

/**
 * Failed tests and line coverage over the service's recent builds.
 *
 * Two small charts, one measure each — never two scales on one axis. Shown
 * only once at least two builds kept reports: one point is not a trend.
 */
export function TestTrend({ serviceId, version, onOpenBuild }) {
  const [points, setPoints] = useState([]);

  useEffect(() => {
    let cancelled = false;
    getCiServiceTestTrend(serviceId, { limit: 20 })
      .then((payload) => !cancelled && setPoints(payload.points || []))
      .catch(() => !cancelled && setPoints([]));
    return () => {
      cancelled = true;
    };
  }, [serviceId, version]);

  const failedValues = useMemo(() => points.map((point) => point.failed), [points]);
  const coverageValues = useMemo(() => points.map((point) => point.linesPct), [points]);
  const anyTests = failedValues.some((value) => value != null);
  const anyCoverage = coverageValues.some((value) => value != null);
  if (points.length < 2 || (!anyTests && !anyCoverage)) return null;

  const last = points[points.length - 1];
  return (
    <div className="sg-ci-trend" aria-label={`Test trend over the last ${points.length} builds with reports`}>
      {anyTests && (
        <TrendChart
          title="Failed tests"
          latest={last.failed != null ? String(last.failed) : "—"}
          tone={last.failed ? "bad" : "ok"}
          kind="bars"
          points={points}
          values={failedValues}
          describe={(point) =>
            point.failed == null
              ? `#${point.number}: no test report`
              : `#${point.number}: ${point.failed} failed of ${point.total}`
          }
          onOpenBuild={onOpenBuild}
        />
      )}
      {anyCoverage && (
        <TrendChart
          title="Line coverage"
          latest={last.linesPct != null ? formatPct(last.linesPct) : "—"}
          kind="line"
          points={points}
          values={coverageValues}
          describe={(point) =>
            point.linesPct == null
              ? `#${point.number}: no coverage report`
              : `#${point.number}: ${formatPct(point.linesPct)} of lines covered`
          }
          onOpenBuild={onOpenBuild}
        />
      )}
      <span className="sg-ci-trend-note muted">last {points.length} builds with reports</span>
    </div>
  );
}

const CHART_W = 132;
const CHART_H = 30;

function TrendChart({ title, latest, tone, kind, points, values, describe, onOpenBuild }) {
  const [hover, setHover] = useState(null);
  const step = CHART_W / points.length;
  const line =
    kind === "line"
      ? sparkline(values, { width: CHART_W, height: CHART_H, pad: 4, padX: step / 2, minSpan: 10 })
      : null;
  const maxValue = Math.max(1, ...values.filter((value) => value != null));
  const shown = hover != null ? points[hover] : null;
  return (
    <div className="sg-ci-trend-chart">
      <div className="sg-ci-trend-head">
        <span className="sg-ci-trend-title">{title}</span>
        <span className={`sg-ci-trend-latest${tone ? ` is-${tone}` : ""}`}>{latest}</span>
      </div>
      <svg
        className={`sg-ci-spark sg-ci-spark--${kind}`}
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        width={CHART_W}
        height={CHART_H}
        role="img"
        aria-label={`${title}, oldest to newest: ${points.map(describe).join("; ")}`}
        onMouseLeave={() => setHover(null)}
      >
        <line className="sg-ci-spark-base" x1="0" x2={CHART_W} y1={CHART_H - 0.5} y2={CHART_H - 0.5} />
        {kind === "bars" &&
          values.map((value, index) => {
            if (value == null) return null;
            const height = value === 0 ? 1.5 : Math.max(3, (value / maxValue) * (CHART_H - 4));
            const width = Math.max(2, step - 2);
            return (
              <rect
                key={points[index].buildId}
                className={value ? "is-bad" : "is-zero"}
                x={index * step + 1}
                y={CHART_H - height}
                width={width}
                height={height}
                rx={Math.min(2, width / 2)}
              />
            );
          })}
        {kind === "line" &&
          line.segments.map((path, index) => <path key={index} className="sg-ci-spark-line" d={path} />)}
        {kind === "line" && line.points[line.points.length - 1] && (
          <circle
            className="sg-ci-spark-dot"
            cx={line.points[line.points.length - 1].x}
            cy={line.points[line.points.length - 1].y}
            r="2.5"
          />
        )}
        {hover != null && (
          <line
            className="sg-ci-spark-cross"
            x1={hover * step + step / 2}
            x2={hover * step + step / 2}
            y1="0"
            y2={CHART_H}
          />
        )}
        {/* Hit targets wider than the marks: a column per build. */}
        {points.map((point, index) => (
          <rect
            key={`hit-${point.buildId}`}
            className="sg-ci-spark-hit"
            x={index * step}
            y="0"
            width={step}
            height={CHART_H}
            onMouseEnter={() => setHover(index)}
            onClick={() => onOpenBuild?.(point.buildId)}
          >
            <title>{describe(point)}</title>
          </rect>
        ))}
      </svg>
      <span className="sg-ci-trend-hover" aria-live="polite">
        {shown ? describe(shown) : " "}
      </span>
    </div>
  );
}
