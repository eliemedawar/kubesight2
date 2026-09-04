import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getCiStageMatrix, rerunCiBuildFrom } from "../../api/ciApi.js";
import { parseApiTime } from "../../lib/apiTime.js";
import EmptyState from "../common/EmptyState.jsx";
import LoadingState from "../common/LoadingState.jsx";
import {
  CheckIcon,
  ClockIcon,
  DotIcon,
  StatusPill,
  TagIcon,
  XIcon,
  formatDuration,
  formatRelative,
  isBuildActive,
  shortSha,
} from "./ciShared.jsx";

const REFRESH_MS = 4000;

/** Twelve rows fit a screen; the table view is the tool for deeper history. */
const PAGE_SIZE = 12;

/**
 * Which of the five skip-ish situations a cell is in.
 *
 * The engine writes one status for three of them, and the whole point of the
 * grid is that they do not look alike: a stage nobody reached is not a problem,
 * a stage the runner could not run is a build that quietly produced nothing.
 */
function cellState(cell) {
  if (!cell) return "void";
  if (cell.status !== "skipped") return cell.status;
  if (cell.skipKind === "not_reached") return "unreached";
  if (cell.skipKind === "reused") return "reused";
  return "skipped";
}

const STATE_WORD = {
  success: "passed",
  running: "running",
  pending: "waiting",
  queued: "waiting",
  failed: "failed",
  timeout: "timed out",
  cancelled: "cancelled",
  skipped: "skipped",
  unreached: "not reached",
  reused: "reused",
  void: "did not exist yet",
};

/** A skip has to be visibly not a failure and not a pass. */
const SlashIcon = () => (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
    <circle cx="8" cy="8" r="6" />
    <path d="M4.5 11.5 11.5 4.5" strokeLinecap="round" />
  </svg>
);

const ReuseIcon = () => (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
    <path d="M13 8a5 5 0 1 1-1.6-3.7" strokeLinecap="round" />
    <path d="M13 2.5V5h-2.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

function StateIcon({ state }) {
  if (state === "success") return <CheckIcon />;
  if (state === "failed" || state === "timeout") return <XIcon />;
  if (state === "running") return <DotIcon />;
  if (state === "skipped") return <SlashIcon />;
  if (state === "reused") return <ReuseIcon />;
  if (state === "cancelled") return <SlashIcon />;
  if (state === "unreached" || state === "void") return null;
  return <ClockIcon />;
}

/**
 * The Stages view: builds as rows, stages as columns.
 *
 * Answers what the flat list cannot — where a pipeline keeps breaking — and
 * keeps the grid on screen while a cell is inspected, because losing it to read
 * one stage is what makes Jenkins' stage view a dead end.
 */
export default function StageMatrix({
  service,
  status = "all",
  canRetry,
  refreshToken,
  onOpenStage,
  onStatusChange,
}) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // An action's failure belongs in the card the action was taken from: the
  // card covers the panel, so a banner behind it would never be read.
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  // Which cell's card is pinned: {rowId, key, rect}.
  const [pinned, setPinned] = useState(null);
  // Ticks while a build is live so a running cell's elapsed time moves. A
  // frozen number is what makes a working build look hung.
  const [now, setNow] = useState(() => Date.now());

  const tableRef = useRef(null);
  const cardRef = useRef(null);
  const timerRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const payload = await getCiStageMatrix(service.id, {
        status: status === "all" ? undefined : status,
        limit: PAGE_SIZE,
      });
      setData(payload);
      setError("");
      return (payload.rows || []).some((row) => isBuildActive(row.status));
    } catch (err) {
      setError(err.message || "Could not load the stage history.");
      return false;
    } finally {
      setLoading(false);
    }
  }, [service.id, status]);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      const active = await load();
      if (cancelled || !active) return;
      timerRef.current = window.setTimeout(tick, REFRESH_MS);
    };
    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timerRef.current);
    };
  }, [load, refreshToken]);

  const rows = data?.rows || [];
  const columns = data?.columns || [];
  const live = rows.some((row) => isBuildActive(row.status));

  useEffect(() => {
    if (!live) return undefined;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [live]);

  // The card is positioned against the viewport, so any scroll or resize moves
  // the cell out from under it. Closing beats chasing it.
  useEffect(() => {
    if (!pinned) return undefined;
    const close = () => setPinned(null);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [pinned]);

  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card || !pinned?.rect) return;
    const { rect } = pinned;
    const height = card.offsetHeight;
    const width = card.offsetWidth;
    const top =
      rect.bottom + 8 + height > window.innerHeight
        ? Math.max(8, rect.top - 8 - height)
        : rect.bottom + 8;
    const left = Math.min(
      Math.max(8, rect.left + rect.width / 2 - width / 2),
      window.innerWidth - width - 8
    );
    card.style.top = `${top}px`;
    card.style.left = `${left}px`;
  }, [pinned]);

  const columnByKey = useMemo(
    () => Object.fromEntries(columns.map((column) => [column.key, column])),
    [columns]
  );

  /** The stage that ends builds early most often — the reason to open a grid. */
  const verdict = useMemo(() => {
    if (rows.length < 2) return null;
    const ranked = columns
      .map((column) => ({ column, score: column.failures + column.skips }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score);
    if (!ranked.length) return null;
    const [{ column, score }] = ranked;
    const runnerUp = ranked[1]?.score || 0;
    const worst = score > runnerUp;
    const of = `${score} of the last ${rows.length} builds`;
    if (column.failures && column.skips) {
      return {
        column,
        text: `${column.name} ended ${of} early — ${column.failures} failed and ${column.skips} could not be run at all.`,
      };
    }
    if (column.failures) {
      return {
        column,
        text: `${column.name} failed in ${of}${worst ? " — more than any other stage" : ""}.`,
      };
    }
    return {
      column,
      text: `${column.name} was skipped in ${of} — those builds produced nothing from it.`,
    };
  }, [columns, rows.length]);

  const elapsed = (cell) => {
    if (cell.durationSeconds != null) return cell.durationSeconds;
    if (cell.status !== "running" || !cell.startedAt) return null;
    const started = parseApiTime(cell.startedAt);
    if (Number.isNaN(started)) return null;
    return Math.max(0, Math.floor((now - started) / 1000));
  };

  const slowFactor = data?.slowFactor || 1.6;
  const slowRatio = (cell, column) => {
    if (!column?.avgSeconds || cell.status !== "success" || cell.durationSeconds == null) {
      return null;
    }
    const ratio = cell.durationSeconds / column.avgSeconds;
    return ratio >= slowFactor ? ratio : null;
  };

  const focusCell = (rowIndex, columnIndex, stepRow, stepColumn) => {
    const table = tableRef.current;
    if (!table) return;
    let r = rowIndex + stepRow;
    let c = columnIndex + stepColumn;
    // Walk past cells for stages a build never had: they are not focusable, and
    // stopping on them would strand the keyboard mid-row.
    while (r >= 0 && r < rows.length && c >= 0 && c < columns.length) {
      const candidate = table.querySelector(`button[data-r="${r}"][data-c="${c}"]`);
      if (candidate) {
        candidate.focus();
        return;
      }
      r += stepRow;
      c += stepColumn;
    }
  };

  const onKeyDown = (event) => {
    const button = event.target.closest?.("button[data-r]");
    if (!button) return;
    const step = {
      ArrowUp: [-1, 0],
      ArrowDown: [1, 0],
      ArrowLeft: [0, -1],
      ArrowRight: [0, 1],
    }[event.key];
    if (step) {
      event.preventDefault();
      focusCell(Number(button.dataset.r), Number(button.dataset.c), step[0], step[1]);
      return;
    }
    if (event.key === "Escape" && pinned) {
      event.preventDefault();
      setPinned(null);
      setActionError("");
    }
  };

  const pinCell = (event, row, key) => {
    setActionError("");
    if (pinned && pinned.rowId === row.id && pinned.key === key) {
      setPinned(null);
      return;
    }
    setPinned({ rowId: row.id, key, rect: event.currentTarget.getBoundingClientRect() });
  };

  const rerun = async (row, cell) => {
    setBusy(true);
    setActionError("");
    try {
      await rerunCiBuildFrom(row.id, cell.position);
      setPinned(null);
      await load();
    } catch (err) {
      setActionError(err.message || "That rerun could not be started.");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <LoadingState label="Loading the stage history…" />;

  if (!rows.length) {
    return (
      <>
        {error && <p className="banner-message error">{error}</p>}
        <EmptyState
          message={
            status === "all" ? "No builds yet." : `No builds with status “${status}”.`
          }
          hint="Run a build to see its stages line up here."
        />
      </>
    );
  }

  const pinnedRow = pinned ? rows.find((row) => row.id === pinned.rowId) : null;
  const pinnedCell = pinnedRow ? pinnedRow.cells[pinned.key] : null;
  const pinnedColumn = pinned ? columnByKey[pinned.key] : null;
  // The last announcement a screen reader needs is one per stage, not one per
  // second — so the live region names the stage, never the elapsed time.
  const runningRow = rows.find((row) => row.status === "running");
  const runningStage = runningRow
    ? Object.values(runningRow.cells).find((cell) => cell?.status === "running")
    : null;

  return (
    <div className="sg-mx">
      {error && <p className="banner-message error">{error}</p>}

      {verdict && (
        <div className="sg-mx-verdict">
          <span aria-hidden="true" className="sg-mx-verdict-mark">
            !
          </span>
          <p>{verdict.text}</p>
          {onStatusChange && status === "all" && verdict.column.failures > 0 && (
            <button
              type="button"
              className="btn-outline btn-compact"
              onClick={() => onStatusChange("failed")}
            >
              Show failed builds
            </button>
          )}
        </div>
      )}

      <p className="sg-mx-sr" aria-live="polite">
        {runningStage
          ? `Build ${runningRow.number}: ${runningStage.name} is running.`
          : ""}
      </p>

      <div className="sg-mx-scroll">
        <table className="sg-mx-table" ref={tableRef} onKeyDown={onKeyDown}>
          <caption className="sg-mx-sr">
            {service.name} builds, newest first. Each cell gives one stage&apos;s status and
            duration. Use the arrow keys to move between cells.
          </caption>
          <thead>
            <tr>
              <th className="sg-mx-gut" scope="col">
                <span className="sg-mx-hdr-label">Build</span>
              </th>
              {columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className={`sg-mx-col${
                    verdict?.column.key === column.key ? " is-hot" : ""
                  }`}
                >
                  <span className="sg-mx-col-name" title={`${column.name} · ${column.stageType}`}>
                    {column.name}
                  </span>
                  <span className="sg-mx-col-avg">
                    {column.avgSeconds != null
                      ? `avg ${formatDuration(column.avgSeconds)}`
                      : "no average yet"}
                  </span>
                  {/* Share of a typical build: the slowest stage falls out of
                      the bar lengths without reading a single number. */}
                  <span className="sg-mx-col-bar" aria-hidden="true">
                    <i
                      style={{
                        width: `${Math.max(3, Math.round((column.shareOfBuild || 0) * 100))}%`,
                      }}
                    />
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={row.id} className={isBuildActive(row.status) ? "is-live" : undefined}>
                <th className="sg-mx-gut" scope="row">
                  <span className="sg-mx-gut-top">
                    <button
                      type="button"
                      className="sg-mx-build"
                      onClick={() => onOpenStage?.(row.id, null)}
                      title="Open this build"
                    >
                      #{row.number}
                    </button>
                    <StatusPill status={row.status} />
                    {row.automation?.deployed && (
                      <span
                        className="status-pill ok"
                        title={`Deployed to ${row.automation.namespace} by run #${row.automation.runId}`}
                      >
                        {row.automation.clusterId}
                      </span>
                    )}
                  </span>
                  <span className="sg-mx-gut-sub">
                    {row.refType === "tag" ? (
                      <span className="sg-ci-ref--tag" title="Built from a git tag">
                        <TagIcon />
                        {row.branch || "—"}
                      </span>
                    ) : (
                      <span>{row.branch || "—"}</span>
                    )}
                    <code>{shortSha(row.commitSha)}</code>
                    <span>{formatRelative(row.finishedAt || row.startedAt || row.queuedAt)}</span>
                    <span>
                      {row.automation
                        ? `automation${
                            row.automation.ticketNumber ? ` · ${row.automation.ticketNumber}` : ""
                          }`
                        : row.requestedBy || "manual"}
                    </span>
                  </span>
                </th>

                {columns.map((column, columnIndex) => {
                  const cell = row.cells[column.key];
                  const state = cellState(cell);
                  if (!cell) {
                    return (
                      <td key={column.key} className="sg-mx-td">
                        <span
                          className="sg-mx-cell sg-mx-cell--void"
                          title={`${column.name} did not exist when build #${row.number} ran`}
                          aria-label={`${column.name}, build ${row.number}: this stage did not exist yet`}
                        />
                      </td>
                    );
                  }
                  const seconds = elapsed(cell);
                  const ratio = slowRatio(cell, column);
                  const isPinned =
                    pinned?.rowId === row.id && pinned?.key === column.key;
                  return (
                    <td key={column.key} className="sg-mx-td">
                      <button
                        type="button"
                        data-r={rowIndex}
                        data-c={columnIndex}
                        className={`sg-mx-cell sg-mx-cell--${state}${isPinned ? " is-pinned" : ""}`}
                        aria-label={`${column.name}, build ${row.number}: ${STATE_WORD[state] || state}${
                          seconds != null ? `, ${formatDuration(seconds)}` : ""
                        }`}
                        aria-expanded={isPinned}
                        onClick={(event) => pinCell(event, row, column.key)}
                      >
                        <span className="sg-mx-glyph" aria-hidden="true">
                          <StateIcon state={state} />
                        </span>
                        <span className="sg-mx-dur">
                          {ratio && <span className="sg-mx-slow">▲ </span>}
                          {seconds != null
                            ? formatDuration(seconds)
                            : state === "skipped"
                            ? "skipped"
                            : state === "reused"
                            ? "reused"
                            : "—"}
                        </span>
                        {state === "running" && (
                          <span className="sg-mx-sweep" aria-hidden="true">
                            <i />
                          </span>
                        )}
                        {cell.continueOnFailure &&
                          ["failed", "timeout"].includes(cell.status) && (
                            <span className="sg-mx-note sg-mx-note--warn">↷ continued</span>
                          )}
                        {ratio && <span className="sg-mx-note">{ratio.toFixed(1)}× usual</span>}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="sg-mx-legend">
        <span>
          <i className="sg-mx-swatch sg-mx-cell--success" /> passed
        </span>
        <span>
          <i className="sg-mx-swatch sg-mx-cell--running" /> running
        </span>
        <span>
          <i className="sg-mx-swatch sg-mx-cell--failed" /> failed
        </span>
        <span>
          <i className="sg-mx-swatch sg-mx-cell--skipped" /> skipped — nothing was produced
        </span>
        <span>
          <i className="sg-mx-swatch sg-mx-cell--unreached" /> not reached
        </span>
        <span>
          <i className="sg-mx-swatch sg-mx-cell--void" /> stage didn&apos;t exist yet
        </span>
      </div>

      {data.total > rows.length && (
        <p className="muted sg-ci-build-note">
          Showing the {rows.length} most recent builds of {data.total}
          {status === "all" ? "" : ` with status “${status}”`}.
        </p>
      )}

      {pinnedCell && pinnedRow && (
        <>
          {/* Click-away without trapping focus: the grid stays usable behind. */}
          <div
            className="sg-mx-card-scrim"
            role="presentation"
            onClick={() => {
              setPinned(null);
              setActionError("");
            }}
          />
          <div
            className="sg-mx-card"
            ref={cardRef}
            role="dialog"
            aria-label={`${pinnedCell.name}, build ${pinnedRow.number}`}
          >
            <div className="sg-mx-card-head">
              <h4>
                {pinnedCell.name} · #{pinnedRow.number}
              </h4>
              {/* Toned like the cell it came from, so the card and the grid
                  never disagree about how alarming a state is. */}
              <span className={`sg-mx-pill sg-mx-cell--${cellState(pinnedCell)}`}>
                {STATE_WORD[cellState(pinnedCell)] || pinnedCell.status}
              </span>
            </div>

            <dl className="sg-mx-card-facts">
              <dt>Duration</dt>
              <dd>{formatDuration(elapsed(pinnedCell))}</dd>
              {pinnedColumn?.avgSeconds != null && (
                <>
                  <dt>Stage average</dt>
                  <dd>
                    {formatDuration(pinnedColumn.avgSeconds)} over {pinnedColumn.sampleSize}{" "}
                    passes
                  </dd>
                </>
              )}
              {pinnedCell.exitCode != null && (
                <>
                  <dt>Exit code</dt>
                  <dd>{pinnedCell.exitCode}</dd>
                </>
              )}
              {pinnedCell.attempt > 1 && (
                <>
                  <dt>Attempt</dt>
                  <dd>{pinnedCell.attempt}</dd>
                </>
              )}
              {pinnedCell.runnerName && (
                <>
                  <dt>Runner</dt>
                  <dd>{pinnedCell.runnerName}</dd>
                </>
              )}
            </dl>

            {/* Why, in the engine's own words wherever it wrote them down. */}
            {pinnedCell.error && <p className="sg-mx-card-why">{pinnedCell.error}</p>}
            {cellState(pinnedCell) === "unreached" && (
              <p className="sg-mx-card-why">
                An earlier stage failed, so this stage never ran.
              </p>
            )}
            {cellState(pinnedCell) === "reused" && (
              <p className="sg-mx-card-why">
                Restored from build #{pinnedCell.reusedFromBuildNumber} — this rerun started
                later in the pipeline.
              </p>
            )}
            {pinnedCell.continueOnFailure &&
              ["failed", "timeout"].includes(pinnedCell.status) && (
                <p className="sg-mx-card-why">
                  This stage is set to continue on failure, so the build carried on past it.
                </p>
              )}

            {pinnedCell.logTail?.length > 0 && (
              <pre className="sg-mx-card-log">
                {pinnedCell.logTail.map((line) => line.content).join("\n")}
              </pre>
            )}

            {actionError && <p className="sg-mx-card-error">{actionError}</p>}

            <div className="sg-mx-card-actions">
              <button
                type="button"
                className="btn-outline btn-compact"
                onClick={() => {
                  setPinned(null);
                  onOpenStage?.(pinnedRow.id, pinnedCell.stageId);
                }}
              >
                Open logs
              </button>
              {/* Position 0 is the checkout, which always runs, so it has no
                  rerun of its own — same rule as the build drawer. */}
              {canRetry && !isBuildActive(pinnedRow.status) && pinnedCell.position > 0 && (
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  disabled={busy}
                  title={`Start a new build at “${pinnedCell.name}”, restoring this build's artifacts`}
                  onClick={() => rerun(pinnedRow, pinnedCell)}
                >
                  Rerun from here
                </button>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
