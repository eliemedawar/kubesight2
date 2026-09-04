import { useCallback, useEffect, useRef, useState } from "react";
import { listCiServiceBuilds } from "../../api/ciApi.js";
import EmptyState from "../common/EmptyState.jsx";
import LoadingState from "../common/LoadingState.jsx";
import BuildDetailDrawer from "./BuildDetailDrawer.jsx";
import StageMatrix from "./StageMatrix.jsx";
import {
  StatusPill,
  TagIcon,
  formatDuration,
  formatRelative,
  isBuildActive,
  shortSha,
} from "./ciShared.jsx";

const REFRESH_MS = 4000;

const STATUS_FILTERS = [
  ["all", "All"],
  ["running", "Running"],
  ["queued", "Queued"],
  ["success", "Success"],
  ["failed", "Failed"],
];

// Ten is what fits on screen without scrolling, and a build history is read
// newest-first — older runs are reached through the status filters.
const PAGE_SIZE = 10;

const VIEWS = [
  ["table", "Table"],
  ["stages", "Stages"],
];

const viewKey = (serviceId) => `ks.ci.buildsview.${serviceId}`;

/**
 * Which view this service opens on.
 *
 * The grid earns its place once there is a history of multi-stage builds to
 * compare; before that it is one row of durations, which the table says better.
 * A choice the user makes is remembered per service and wins from then on.
 */
function initialView(service) {
  try {
    const saved = window.localStorage.getItem(viewKey(service.id));
    if (saved === "table" || saved === "stages") return saved;
  } catch {
    // Private mode or blocked storage: fall through to the default.
  }
  return (service.pipelineStageCount || 0) >= 2 && (service.buildCount || 0) >= 2
    ? "stages"
    : "table";
}

/**
 * Builds tab: the history, in either of its two readings, plus the drawer.
 *
 * Table answers "what happened in this build"; Stages answers "where does this
 * pipeline keep breaking". Both share the status filter and the same drawer, so
 * switching never loses the user's place.
 */
export default function BuildsPanel({ service, canCancel, canRetry, refreshToken }) {
  const [view, setView] = useState(() => initialView(service));
  const [builds, setBuilds] = useState([]);
  const [queueDepth, setQueueDepth] = useState(0);
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [openBuild, setOpenBuild] = useState(null);
  const timerRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const data = await listCiServiceBuilds(service.id, {
        status: status === "all" ? undefined : status,
        limit: PAGE_SIZE,
      });
      setBuilds(data.items || []);
      setQueueDepth(data.queueDepth || 0);
      setError("");
      return (data.items || []).some((build) => isBuildActive(build.status));
    } catch (err) {
      setError(err.message || "Could not load builds.");
      return false;
    } finally {
      setLoading(false);
    }
  }, [service.id, status]);

  // Only the visible view polls. The grid fetches its own payload, so keeping
  // the table's loop alive underneath it would double the traffic for nothing.
  useEffect(() => {
    if (view !== "table") return undefined;
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
  }, [load, refreshToken, view]);

  const chooseView = (next) => {
    setView(next);
    try {
      window.localStorage.setItem(viewKey(service.id), next);
    } catch {
      // Not remembering the choice is survivable; failing to switch is not.
    }
  };

  const toolbar = (
    <div className="sg-cat-toolbar">
      <div className="sg-cat-tabs" role="group" aria-label="Filter builds by status">
        {STATUS_FILTERS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={`sg-cat-tab${status === value ? " is-on" : ""}`}
            aria-pressed={status === value}
            onClick={() => setStatus(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {/* Queue depth comes with the list response, so it is only ever shown by
          the view that fetched it. */}
      {view === "table" && queueDepth > 0 && (
        <span className="muted">
          {queueDepth} build{queueDepth === 1 ? "" : "s"} waiting in the queue
        </span>
      )}
      <div
        className="sg-cat-tabs sg-ci-view-switch"
        role="group"
        aria-label="How to read the build history"
      >
        {VIEWS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={`sg-cat-tab${view === value ? " is-on" : ""}`}
            aria-pressed={view === value}
            onClick={() => chooseView(value)}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );

  const drawer = openBuild && (
    <BuildDetailDrawer
      buildId={openBuild.buildId}
      initialStageId={openBuild.stageId}
      onClose={() => setOpenBuild(null)}
      onChanged={load}
      canCancel={canCancel}
      canRetry={canRetry}
    />
  );

  if (view === "stages") {
    return (
      <div className="sg-ci-panel">
        {toolbar}
        <StageMatrix
          service={service}
          status={status}
          canRetry={canRetry}
          refreshToken={refreshToken}
          onOpenStage={(buildId, stageId) => setOpenBuild({ buildId, stageId })}
          onStatusChange={setStatus}
        />
        {drawer}
      </div>
    );
  }

  if (loading) return <LoadingState label="Loading builds…" />;

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      {toolbar}

      {builds.length === 0 ? (
        <EmptyState
          message="No builds yet."
          hint="Run a build to see it here with its stages and logs."
        />
      ) : (
        <div className="table-wrap">
          <table className="data-table sg-ci-build-table">
            <thead>
              <tr>
                <th>Build</th>
                <th>Ref</th>
                <th>Commit</th>
                <th>Status</th>
                <th>Duration</th>
                <th>When</th>
                <th>Trigger</th>
                <th>Deployed</th>
              </tr>
            </thead>
            <tbody>
              {builds.map((build) => (
                <tr
                  key={build.id}
                  className="sg-ci-build-row"
                  tabIndex={0}
                  role="button"
                  aria-label={`Open build ${build.number}`}
                  onClick={() => setOpenBuild({ buildId: build.id, stageId: null })}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setOpenBuild({ buildId: build.id, stageId: null });
                    }
                  }}
                >
                  <td>
                    <strong>#{build.number}</strong>
                  </td>
                  <td>
                    {/* A tag build says so — "v1.72.1" alone reads like a branch. */}
                    {build.refType === "tag" ? (
                      <span className="sg-ci-ref--tag" title="Built from a git tag">
                        <TagIcon />
                        {build.branch || "—"}
                      </span>
                    ) : (
                      build.branch || "—"
                    )}
                  </td>
                  <td>
                    <code>{shortSha(build.commitSha)}</code>
                  </td>
                  <td>
                    <StatusPill status={build.status} />
                    {build.status === "queued" && build.queueReason && (
                      <span className="field-hint">{build.queueReason}</span>
                    )}
                  </td>
                  <td>{formatDuration(build.durationSeconds)}</td>
                  <td>{formatRelative(build.finishedAt || build.startedAt || build.queuedAt)}</td>
                  <td>
                    {/* Provenance: WHO asked for this build. Ticket-driven
                        builds show their ticket — the org's CI runs itself. */}
                    {build.automation ? (
                      <span
                        className="sg-tag sg-ci-tag--auto"
                        title={`Deploy automation run #${build.automation.runId}`}
                      >
                        automation{build.automation.ticketNumber ? ` · ${build.automation.ticketNumber}` : ""}
                      </span>
                    ) : build.triggerType === "retry" ? (
                      <span className="chip">retry{build.requestedBy ? ` · ${build.requestedBy}` : ""}</span>
                    ) : (
                      <span className="muted">manual{build.requestedBy ? ` · ${build.requestedBy}` : ""}</span>
                    )}
                  </td>
                  <td>
                    {/* Closes the chain: commit → build → artifact → deploy. */}
                    {build.automation?.deployed ? (
                      <span
                        className="status-pill ok"
                        title={`Deployed to ${build.automation.namespace} by run #${build.automation.runId}`}
                      >
                        {build.automation.clusterId}
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* Say it rather than let a truncated list read as the whole history. */}
          {builds.length === PAGE_SIZE && (
            <p className="muted sg-ci-build-note">
              Showing the {PAGE_SIZE} most recent builds
              {status === "all" ? "" : ` with status “${status}”`}.
            </p>
          )}
        </div>
      )}

      {drawer}
    </div>
  );
}
