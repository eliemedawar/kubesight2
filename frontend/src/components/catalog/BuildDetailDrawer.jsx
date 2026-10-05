import { useEffect, useRef, useState } from "react";
import { parseApiTime } from "../../lib/apiTime.js";
import {
  cancelCiBuild,
  getCiBuild,
  listCiBuildArtifacts,
  approveCiBuildStage,
  rejectCiBuildStage,
  retryCiBuild,
} from "../../api/ciApi.js";
import ApprovalStagePanel from "./ApprovalStagePanel.jsx";
import CodeScanReportPanel from "./CodeScanReportPanel.jsx";
import StoreUploadStageSummary from "./StoreUploadStageSummary.jsx";
import PipelineStrip from "./PipelineStrip.jsx";
import { buildSteps } from "./pipeline/parallelModel.js";
import PostActionRunPanel, { postRunState } from "./PostActionRunPanel.jsx";
import StageLogViewer from "./StageLogViewer.jsx";
import TestResultsPanel from "./TestResultsPanel.jsx";
import WorkspaceBrowser from "./WorkspaceBrowser.jsx";
import {
  StageStatusIcon,
  StatusPill,
  TagIcon,
  formatDuration,
  formatRelative,
  isBuildActive,
  shortSha,
} from "./ciShared.jsx";

// The drawer is open on one build the user is watching stage by stage, and it
// stops polling as soon as that build finishes.
const REFRESH_MS = 1200;
// ...unless a post-action notification is still on its way (queued, sending,
// waiting out a retry): those settle after the build, so keep looking, slowly.
const SETTLE_MS = 4000;
const settling = (build) =>
  (build?.postActions || []).some((item) => item.status === "pending" || item.status === "running");

/**
 * One build: header, stage list, and the selected stage's logs.
 *
 * Polls only while the build is active, then stops — a finished build is
 * immutable, so there is nothing to refresh.
 */
export default function BuildDetailDrawer({
  buildId,
  initialStageId,
  onClose,
  onChanged,
  canCancel,
  canRetry,
}) {
  const [build, setBuild] = useState(null);
  const [artifacts, setArtifacts] = useState([]);
  // How many the build actually owns. A glob-heavy stage declares one artifact
  // per matched file, so this can run far ahead of what the API returns.
  const [artifactTotal, setArtifactTotal] = useState(0);
  const [selectedStageId, setSelectedStageId] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // The workspace replaces the log pane rather than sitting beside it: both
  // answer "what is this build doing", and the drawer has one pane.
  const [showWorkspace, setShowWorkspace] = useState(false);
  // Ticks once a second while the build is live so every duration on screen
  // moves. A stage that has started but not finished has no server-side
  // duration yet, and a frozen "—" is what makes a working build look hung.
  const [now, setNow] = useState(() => Date.now());

  // Follows the running stage until the user picks one themselves.
  const userPickedRef = useRef(false);
  const timerRef = useRef(null);

  useEffect(() => {
    // Opened on a specific stage (a cell in the stage matrix): that is a
    // choice, so auto-follow must not move off it.
    userPickedRef.current = Boolean(initialStageId);
    setSelectedStageId(initialStageId || null);
    setBuild(null);
  }, [buildId, initialStageId]);

  useEffect(() => {
    let cancelled = false;
    let artifactsLoaded = false;

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
          if (!artifactsLoaded) {
            artifactsLoaded = true;
            const list = await listCiBuildArtifacts(buildId);
            if (!cancelled) {
              const items = list.items || [];
              setArtifacts(items);
              setArtifactTotal(list.total ?? items.length);
            }
          }
          if (!cancelled && settling(data)) timerRef.current = window.setTimeout(load, SETTLE_MS);
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

  // Keyed on the status, not the build object: the poll replaces that object
  // every couple of seconds, which would tear down and rebuild the interval.
  useEffect(() => {
    if (!build?.status || !isBuildActive(build.status)) return undefined;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [build?.status]);

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
    setShowWorkspace(false);
  };

  // A post-action row is selected like a stage: its log is a stage log.
  const selectedPost = build?.postActions?.find((item) => item.id === selectedStageId);
  const selectedStage = build?.stages?.find((stage) => stage.id === selectedStageId) || selectedPost;
  const active = build ? isBuildActive(build.status) : false;

  // Elapsed time for a stage the server has not timed yet.
  const stageDuration = (stage) => {
    if (stage.durationSeconds != null) return formatDuration(stage.durationSeconds);
    if (stage.status !== "running" || !stage.startedAt) return formatDuration(null);
    const started = parseApiTime(stage.startedAt);
    if (Number.isNaN(started)) return formatDuration(null);
    return formatDuration(Math.max(0, Math.floor((now - started) / 1000)));
  };

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
                {/* Provenance: a ticket-driven build names its ticket here,
                    a scheduled one its schedule (and whose rights it ran with). */}
                {build.automation
                  ? ` · automation${
                      build.automation.ticketNumber
                        ? ` (ticket ${build.automation.ticketNumber})`
                        : ""
                    }`
                  : build.triggerType === "schedule"
                  ? ` · schedule “${build.schedule?.name || "?"}”${
                      build.requestedBy
                        ? build.schedule?.manual
                          ? `, run now by ${build.requestedBy}`
                          : ` as ${build.requestedBy}`
                        : ""
                    }`
                  : build.requestedBy
                  ? ` · by ${build.requestedBy}`
                  : ""}{" "}
                ·{" "}
                {build.durationSeconds != null
                  ? formatDuration(build.durationSeconds)
                  : build.startedAt && !Number.isNaN(parseApiTime(build.startedAt))
                  ? formatDuration(
                      Math.max(0, Math.floor((now - parseApiTime(build.startedAt)) / 1000))
                    )
                  : formatDuration(null)}
                {build.automation?.deployed && (
                  <>
                    {" "}
                    · <span className="status-pill ok">deployed → {build.automation.clusterId}</span>
                  </>
                )}
                {build.sharedPipeline && (
                  <>
                    {" "}
                    ·{" "}
                    <span className="chip" title="Ran the stages of a pipeline from the Pipelines page, at this version">
                      shared pipeline {build.sharedPipeline.name} v{build.sharedPipeline.version}
                    </span>
                  </>
                )}
              </p>
            )}
          </div>
          <div className="sg-ci-drawer-actions">
            {build && active && (
              <button
                type="button"
                className={`btn-outline btn-compact${showWorkspace ? " is-on" : ""}`}
                aria-pressed={showWorkspace}
                onClick={() => setShowWorkspace((prev) => !prev)}
                title="Browse the files this build has produced so far"
              >
                {showWorkspace ? "Logs" : "Workspace"}
              </button>
            )}
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
        {build?.awaitingApproval && selectedStageId !== build.awaitingApproval.stageId && (
          <p className="banner-message info sg-ci-awaiting">
            Waiting for approval at “{build.awaitingApproval.stageName}” —{" "}
            {build.awaitingApproval.approvals} of {build.awaitingApproval.required}.{" "}
            <button
              type="button"
              className="btn-outline btn-compact"
              onClick={() => {
                const stage = build.stages?.find((item) => item.id === build.awaitingApproval.stageId);
                if (stage) selectStage(stage);
              }}
            >
              Open it
            </button>
          </p>
        )}

        {build?.parallel?.mode === "sequential" && (
          <p className="banner-message info">
            This build ran its parallel groups one stage at a time: {build.parallel.reason}
          </p>
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
                {buildSteps(build.stages || []).map((step) => {
                  const row = (stage) => (
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
                        <span className="sg-ci-stage-time">{stageDuration(stage)}</span>
                      </button>
                    </li>
                  );
                  if (!step.group) return row(step.items[0]);
                  // A parallel group: its members ran at the same time, so
                  // they hang off one bracket instead of reading as a sequence.
                  const running = step.items.filter((stage) => stage.status === "running").length;
                  return (
                    <li key={`group-${step.items[0].id}`} className="sg-ci-stage-group">
                      <div className="sg-ci-stage-group-head">
                        <span className="sg-ci-stage-group-name">{step.group}</span>
                        <span className="sg-ci-stage-group-meta">
                          {build.parallel?.mode === "sequential"
                            ? "one at a time"
                            : running > 1
                              ? `${running} running at once`
                              : `${step.items.length} together`}
                          {step.failFast ? " · fail fast" : ""}
                        </span>
                      </div>
                      <ul className="sg-ci-stage-lanes">{step.items.map(row)}</ul>
                    </li>
                  );
                })}
                {/* Post actions: not stages — they never decide the build's
                    result — so they sit under their own heading. */}
                {(build.postActions || []).length > 0 && (
                  <li className="sg-ci-post-heading" aria-hidden="true">
                    When the build ended
                  </li>
                )}
                {(build.postActions || []).map((item) => (
                  <li key={`post-${item.id}`}>
                    <button
                      type="button"
                      className={`sg-ci-stage-row sg-ci-stage-row--${item.status} sg-ci-post-row${
                        item.id === selectedStageId ? " is-active" : ""
                      }`}
                      onClick={() => selectStage(item)}
                      title={item.error || item.detail || item.name}
                    >
                      <span className={`sg-ci-stage-icon sg-ci-stage-icon--${item.status}`}>
                        <StageStatusIcon status={item.status} />
                      </span>
                      <span className="sg-ci-stage-name">{item.name}</span>
                      <span className={`sg-ci-post-state is-${item.status}`}>{postRunState(item)}</span>
                    </button>
                  </li>
                ))}
              </ul>

              <div className="sg-ci-drawer-logs">
                {showWorkspace ? (
                  <WorkspaceBrowser buildId={build.id} active={active} />
                ) : selectedStage ? (
                  <>
                    {selectedStage.codeScan && (
                      <CodeScanReportPanel buildId={build.id} stage={selectedStage} />
                    )}
                    {selectedStage.stageType === "approval" && (
                      <ApprovalStagePanel
                        stage={selectedStage}
                        busy={busy}
                        onDecide={(action, comment) =>
                          act(() =>
                            (action === "approve" ? approveCiBuildStage : rejectCiBuildStage)(
                              build.id,
                              selectedStage.id,
                              comment
                            )
                          )
                        }
                      />
                    )}
                    {selectedStage.stageType === "store_upload" && (
                      <StoreUploadStageSummary stage={selectedStage} />
                    )}
                    {selectedPost && <PostActionRunPanel item={selectedPost} />}
                    <StageLogViewer buildId={build.id} stage={selectedStage} />
                  </>
                ) : (
                  <p className="muted">Select a stage to see its output.</p>
                )}
              </div>
            </div>

            <TestResultsPanel build={build} />

            {artifacts.length > 0 && (
              <section className="sg-ci-drawer-artifacts">
                <p className="form-label">Artifacts ({artifactTotal})</p>
                <ul>
                  {artifacts.map((artifact) => (
                    <li key={artifact.id}>
                      <span className="chip">{artifact.artifactType}</span>
                      {/* The full path only fits in the tooltip once a build
                          starts producing deeply nested output. */}
                      <code title={artifact.uri || artifact.name}>
                        {artifact.uri || artifact.name}
                      </code>
                      {artifact.digest && (
                        <span className="muted"> {artifact.digest.slice(0, 19)}…</span>
                      )}
                    </li>
                  ))}
                </ul>
                {artifactTotal > artifacts.length && (
                  <p className="field-hint">
                    Showing the first {artifacts.length}. Every artifact is listed on the
                    service's Artifacts tab.
                  </p>
                )}
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
