import { useCallback, useEffect, useRef, useState } from "react";
import { listCiServiceBuilds } from "../../api/ciApi.js";
import EmptyState from "../common/EmptyState.jsx";
import LoadingState from "../common/LoadingState.jsx";
import BuildDetailDrawer from "./BuildDetailDrawer.jsx";
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

/**
 * Builds tab: the list, plus the drawer.
 *
 * Auto-refreshes only while at least one build is active, so a service whose
 * builds have all finished stops polling entirely.
 */
export default function BuildsPanel({ service, canCancel, canRetry, refreshToken }) {
  const [builds, setBuilds] = useState([]);
  const [queueDepth, setQueueDepth] = useState(0);
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [openBuildId, setOpenBuildId] = useState(null);
  const timerRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const data = await listCiServiceBuilds(service.id, {
        status: status === "all" ? undefined : status,
        limit: 50,
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

  if (loading) return <LoadingState label="Loading builds…" />;

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

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
        {queueDepth > 0 && (
          <span className="muted">
            {queueDepth} build{queueDepth === 1 ? "" : "s"} waiting in the queue
          </span>
        )}
      </div>

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
                  onClick={() => setOpenBuildId(build.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setOpenBuildId(build.id);
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
        </div>
      )}

      {openBuildId && (
        <BuildDetailDrawer
          buildId={openBuildId}
          onClose={() => setOpenBuildId(null)}
          onChanged={load}
          canCancel={canCancel}
          canRetry={canRetry}
        />
      )}
    </div>
  );
}
