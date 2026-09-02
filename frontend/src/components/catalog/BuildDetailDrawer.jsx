import { useEffect, useRef, useState } from "react";
import { cancelCiBuild, getCiBuild, listCiBuildArtifacts, retryCiBuild } from "../../api/ciApi.js";
import PipelineStrip from "./PipelineStrip.jsx";
import StageLogViewer from "./StageLogViewer.jsx";
import {
  StageStatusIcon,
  StatusPill,
  TagIcon,
  formatDuration,
  formatRelative,
  isBuildActive,
  shortSha,
} from "./ciShared.jsx";

const REFRESH_MS = 2500;

/**
 * One build: header, stage list, and the selected stage's logs.
 *
 * Polls only while the build is active, then stops — a finished build is
 * immutable, so there is nothing to refresh.
 */
export default function BuildDetailDrawer({
  buildId,
  onClose,
  onChanged,
  canCancel,
  canRetry,
}) {
  const [build, setBuild] = useState(null);
  const [artifacts, setArtifacts] = useState([]);
  const [selectedStageId, setSelectedStageId] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Follows the running stage until the user picks one themselves.
  const userPickedRef = useRef(false);
  const timerRef = useRef(null);

  useEffect(() => {
    userPickedRef.current = false;
    setSelectedStageId(null);
    setBuild(null);
  }, [buildId]);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const data = await getCiBuild(buildId);
        if (cancelled) return;
        setBuild(data);
        setError("");

        if (!userPickedRef.current) {
          const active = data.stages?.find((stage) => stage.status === "running");
          const lastInteresting =
            active ||
            [...(data.stages || [])].reverse().find((stage) =>
              ["failed", "timeout", "success"].includes(stage.status)
            );
          if (lastInteresting) setSelectedStageId(lastInteresting.id);
        }

        if (!isBuildActive(data.status)) {
          const list = await listCiBuildArtifacts(buildId);
          if (!cancelled) setArtifacts(list.items || []);
          return;
        }
        timerRef.current = window.setTimeout(load, REFRESH_MS);
      } catch (err) {
        if (!cancelled) setError(err.message || "Could not load the build.");
      }
    };
    load();

    return () => {
      cancelled = true;
      window.clearTimeout(timerRef.current);
    };
  }, [buildId]);

  const act = async (action) => {
    setBusy(true);
    setError("");
    try {
      await action();
      const refreshed = await getCiBuild(buildId);
      setBuild(refreshed);
      onChanged?.();
    } catch (err) {
      setError(err.message || "That action failed.");
    } finally {
      setBusy(false);
    }
  };

  const selectStage = (stage) => {
    userPickedRef.current = true;
    setSelectedStageId(stage.id);
  };

  const selectedStage = build?.stages?.find((stage) => stage.id === selectedStageId);
  const active = build ? isBuildActive(build.status) : false;

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <aside
        className="sg-ci-drawer"
        role="dialog"
        aria-label="Build details"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="sg-ci-drawer-head">
          <div>
            <h3>
              Build #{build?.number ?? "…"}
              {build && <StatusPill status={build.status} />}
            </h3>
            {build && (
              <p className="muted sg-ci-drawer-sub">
                {build.serviceName} ·{" "}
                {build.refType === "tag" ? (
                  <span className="sg-ci-ref--tag" title="Built from a git tag">
                    <TagIcon />
                    {build.branch || "—"}
                  </span>
                ) : (
                  build.branch || "—"
                )}{" "}
                ·{" "}
                {build.commitSha ? <code>{shortSha(build.commitSha)}</code> : "no commit pinned"}
                {/* Provenance: a ticket-driven build names its ticket here. */}
                {build.automation
                  ? ` · automation${
                      build.automation.ticketNumber
                        ? ` (ticket ${build.automation.ticketNumber})`
                        : ""
                    }`
                  : build.requestedBy
                  ? ` · by ${build.requestedBy}`
                  : ""}{" "}
                · {formatDuration(build.durationSeconds)}
                {build.automation?.deployed && (
                  <>
                    {" "}
                    · <span className="status-pill ok">deployed → {build.automation.clusterId}</span>
                  </>
                )}
              </p>
            )}
          </div>
          <div className="sg-ci-drawer-actions">
            {build && active && canCancel && (
              <button
                type="button"
                className="btn-outline btn-compact danger"
                disabled={busy || build.cancelRequested}
                onClick={() => act(() => cancelCiBuild(build.id))}
              >
                {build.cancelRequested ? "Cancelling…" : "Cancel"}
              </button>
            )}
            {build && !active && canRetry && (
              <button
                type="button"
                className="btn-outline btn-compact"
                disabled={busy}
                onClick={() => act(() => retryCiBuild(build.id))}
              >
                Retry
              </button>
            )}
            <button type="button" className="btn-outline btn-compact" onClick={onClose}>
              Close
            </button>
          </div>
        </header>

        {error && <p className="banner-message error">{error}</p>}
        {build?.error && <p className="banner-message error">{build.error}</p>}
        {build?.status === "queued" && build.queueReason && (
          <p className="banner-message info">{build.queueReason}</p>
        )}

        {build && (
          <>
            <PipelineStrip
              stages={build.stages || []}
              activeStageId={selectedStageId}
              onSelectStage={selectStage}
            />

            <div className="sg-ci-drawer-body">
              <ul className="sg-ci-stage-list">
                {(build.stages || []).map((stage) => (
                  <li key={stage.id}>
                    <button
                      type="button"
                      className={`sg-ci-stage-row sg-ci-stage-row--${stage.status}${
                        stage.id === selectedStageId ? " is-active" : ""
                      }`}
                      onClick={() => selectStage(stage)}
                    >
                      <span className={`sg-ci-stage-icon sg-ci-stage-icon--${stage.status}`}>
                        <StageStatusIcon status={stage.status} />
                      </span>
                      <span className="sg-ci-stage-name">{stage.name}</span>
                      <span className="sg-ci-stage-time">
                        {formatDuration(stage.durationSeconds)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>

              <div className="sg-ci-drawer-logs">
                {selectedStage ? (
                  <StageLogViewer buildId={build.id} stage={selectedStage} />
                ) : (
                  <p className="muted">Select a stage to see its output.</p>
                )}
              </div>
            </div>

            {artifacts.length > 0 && (
              <section className="sg-ci-drawer-artifacts">
                <p className="form-label">Artifacts ({artifacts.length})</p>
                <ul>
                  {artifacts.map((artifact) => (
                    <li key={artifact.id}>
                      <span className="chip">{artifact.artifactType}</span>
                      <code>{artifact.uri || artifact.name}</code>
                      {artifact.digest && (
                        <span className="muted"> {artifact.digest.slice(0, 19)}…</span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <p className="field-hint">
              Queued {formatRelative(build.queuedAt)}
              {build.runnerName ? ` · runner ${build.runnerName}` : ""}
              {build.retryOfBuildId ? ` · retry of build ${build.retryOfBuildId}` : ""}
            </p>
          </>
        )}
      </aside>
    </div>
  );
}
