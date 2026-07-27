/** One build, from the moment it starts to the cluster it becomes.
 *
 *  Three changes carry this screen: the phase rail runs across the top instead
 *  of down the page, the Blueprint reports which machines have actually joined,
 *  and a failure gets a hero — where it stopped, the command, the cause, and
 *  what retry will do — rather than a red border on a card you have to find.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Blueprint from "./Blueprint.jsx";
import GrowPanel from "./GrowPanel.jsx";
import PhaseRail from "./PhaseRail.jsx";
import { AddonChips, LiveBadge, StatusPill } from "./common.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { parseApiTime } from "../../lib/apiTime";
import {
  PHASE_LABELS,
  PHASE_NOTES,
  ROLE_LABELS,
  buildBlueprint,
  buildDuration,
  buildProgress,
  failurePoint,
  formatClock,
  isGrowing,
  runStartedAt,
  addonDisplayName,
} from "../../utils/clusterBuilder.js";
import {
  cancelClusterBuild,
  deleteClusterBuild,
  getClusterBuild,
  getClusterBuildKubeconfig,
  getClusterBuildLogs,
  retryClusterBuild,
} from "../../api/clusterBuildsApi.js";

const DETAIL_POLL_INTERVAL_MS = 2500;
const LOG_REFRESH_MS = 2500;

function stepLabel(step, nodeById) {
  if (!step.nodeId) return "cluster";
  const node = nodeById[step.nodeId];
  return node?.hostname || node?.address || `node ${step.nodeId}`;
}

/** Where it stopped and what happens next — the whole failure in one block. */
function FailureHero({ point, build, log, canExecute, onRetry, busy }) {
  const error = point.step.error || build.error || "";
  return (
    <div className="card sg-cb-blowup">
      <div>
        <h3>Stopped at {point.phaseLabel.toLowerCase()}</h3>
        <p className="sg-cb-where sg-cb-mono">
          phase {point.position} of {point.total} · {point.phaseLabel}
          {point.node ? ` · ${point.node.hostname || point.node.address}` : ""}
          {point.step.attempt > 1 ? ` · attempt ${point.step.attempt}` : ""}
        </p>
      </div>
      {error ? <pre className="sg-cb-cmdline">{error}</pre> : null}
      {log?.logTail ? (
        <div className="sg-cb-cause">
          <span aria-hidden="true" className="sg-cb-cause-mark">▲</span>
          <span>
            <b>Last output before it stopped.</b> The full log for this step is below —
            secrets were scrubbed before anything was written down.
          </span>
        </div>
      ) : null}
      <div className="sg-cb-blowup-acts">
        {canExecute ? (
          <button className="primary" type="button" disabled={busy} onClick={onRetry}>
            {build.status === "cancelled"
              ? "Resume — completed phases are kept"
              : "Retry from here"}
          </button>
        ) : null}
        <span className="sg-cb-safe">
          {point.completedPhases} of {point.total} phases completed · nothing was rolled back ·
          {" "}fresh join secrets are minted on retry
        </span>
      </div>
    </div>
  );
}

function DayTwo({
  build, canCreate, canDownloadKubeconfig, onOpenCluster, onGrow, notify, busy, setBusy,
}) {
  const download = async () => {
    setBusy(true);
    try {
      const { filename, content } = await getClusterBuildKubeconfig(build.id);
      // Handing this to the browser rather than opening it: it is cluster-admin
      // credentials and should not sit in a tab's history.
      const url = URL.createObjectURL(new Blob([content], { type: "application/yaml" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Revoking in the same tick as the click can cancel the download before
      // the browser has read the blob.
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      notify(`${filename} downloaded — this is cluster-admin access, and the download was recorded.`);
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sg-cb-day2">
      <span className="sg-cb-day2-label">Day two</span>
      {canCreate ? (
        <button className="btn-outline btn-sm" type="button" onClick={onGrow}>
          Add worker machines
        </button>
      ) : null}
      {canDownloadKubeconfig ? (
        <button className="btn-outline btn-sm" type="button" disabled={busy} onClick={download}>
          Download kubeconfig
        </button>
      ) : null}
      {onOpenCluster && build.resultClusterId ? (
        <button
          className="btn-outline btn-sm"
          type="button"
          onClick={() => onOpenCluster(build.resultClusterId)}
        >
          Open in Clusters
        </button>
      ) : null}
      {canDownloadKubeconfig ? (
        <span className="muted sg-cb-day2-note">
          The kubeconfig is cluster-admin access outside KubeSight&apos;s own permissions —
          every download is written to the audit log.
        </span>
      ) : null}
    </div>
  );
}

function Receipt({
  build, catalog, duration, addonStep, onOpenAddonLog, dayTwo,
}) {
  const machineCount = (build.nodes || []).length;
  return (
    <>
      <div className="card sg-cb-receipt">
        <div className="sg-cb-okring" aria-hidden="true">✓</div>
        <div>
          <h3>{build.name} is alive</h3>
          <p className="muted">
            {machineCount} machine{machineCount === 1 ? "" : "s"}, registered in KubeSight and
            already visible in Clusters, Dashboard and Inventory. Nothing is left to do by hand.
          </p>
        </div>
      </div>

      {dayTwo}

      <div className="sg-cb-facts">
        <div className="sg-cb-fact">
          <div className="k">Endpoint</div>
          <div className="v sg-cb-mono">{build.controlPlaneEndpoint}</div>
        </div>
        {duration ? (
          <div className="sg-cb-fact"><div className="k">Build time</div><div className="v">{duration}</div></div>
        ) : null}
        <div className="sg-cb-fact">
          <div className="k">Network</div>
          <div className="v">{build.cniPlugin} · <span className="sg-cb-mono">{build.podCidr}</span></div>
        </div>
        <div className="sg-cb-fact">
          <div className="k">Join secrets</div>
          <div className="v">Destroyed after use</div>
        </div>
      </div>

      {(build.addons || []).length ? (
        <div className="card sg-cb-card">
          <div className="sg-cb-sect">
            <h2>Add-ons — verified, not just applied</h2>
            <span className="sg-cb-sect-right">
              {addonStep ? <StatusPill status={addonStep.status} /> : null}
            </span>
          </div>
          <ul className="sg-cb-proofs">
            {(build.addons || []).map((addon) => (
              <li className="sg-cb-proof" key={typeof addon === "string" ? addon : addon.id}>
                <span className="tick" aria-hidden="true">✓</span>
                <span className="what">
                  {addonDisplayName(addon, catalog)}
                  {typeof addon === "object" && addon.version ? ` v${addon.version}` : ""}
                </span>
                <span className="how sg-cb-mono">
                  {Object.values((typeof addon === "object" && addon.config) || {})
                    .map((entry) => (Array.isArray(entry) ? entry.join(", ") : entry))
                    .filter(Boolean)
                    .join(" · ") || "applied and waited for readiness"}
                </span>
              </li>
            ))}
          </ul>
          <p className="muted sg-cb-proof-note">
            The add-ons phase does not finish until each one answers for real — metrics for
            every node, the ingress NodePort serving HTTP, a probe Service handed an address
            inside the MetalLB pool.{" "}
            {addonStep ? (
              <button className="sg-cb-linkbtn" type="button" onClick={onOpenAddonLog}>
                Open the proof in the phase log
              </button>
            ) : null}
          </p>
        </div>
      ) : null}
    </>
  );
}

export default function BuildDetail({
  buildId,
  canCreate = false,
  canExecute,
  canDownloadKubeconfig = false,
  onOpenCluster = null,
  notify,
  onBack,
  onDeleted,
  addonCatalog = [],
  buildProfiles = [],
}) {
  const [growing, setGrowing] = useState(false);
  const [build, setBuild] = useState(null);
  const [logs, setLogs] = useState(null);
  // The step being viewed: {id, nodeId, phase}.
  const [logStep, setLogStep] = useState(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);
  // While the build runs, the log panel tracks the first running step until
  // someone chooses a lane. A manual click pins that exact step, which matters
  // in parallel phases where the generic follower would otherwise jump back.
  const follow = useRef(true);
  const previousViewedStatus = useRef(null);
  const logEl = useRef(null);
  const stickToBottom = useRef(true);
  const failureOpened = useRef(false);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    try {
      const data = await getClusterBuild(buildId);
      setBuild(data);
      return data;
    } catch (error) {
      notify(error.message, true);
      return null;
    }
  }, [buildId, notify]);

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      const data = await load();
      if (stopped) return;
      if (data && (data.status === "building" || data.status === "preflighting")) {
        timer.current = setTimeout(tick, DETAIL_POLL_INTERVAL_MS);
      }
    };
    tick();
    return () => { stopped = true; clearTimeout(timer.current); };
  }, [load]);

  const isRunning = build?.status === "building" || build?.status === "preflighting";

  useEffect(() => {
    if (!isRunning) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isRunning]);

  const fetchLog = useCallback(async (step) => {
    try {
      const data = await getClusterBuildLogs(buildId, step.nodeId || undefined);
      const match = (data.items || []).find((item) => item.id === step.id);
      setLogs(match
        ? { ...match, phase: step.phase }
        : { logTail: "(waiting for output…)", phase: step.phase });
    } catch (error) {
      notify(error.message, true);
    }
  }, [buildId, notify]);

  const openLogs = useCallback((step, { auto = false } = {}) => {
    if (!auto) follow.current = false;
    const meta = { id: step.id, nodeId: step.nodeId, phase: step.phase };
    previousViewedStatus.current = step.status;
    stickToBottom.current = true;
    setLogStep(meta);
    fetchLog(meta);
  }, [fetchLog]);

  // Live-follow: keep the panel on the running step, including advancing to the
  // next one when a phase completes.
  useEffect(() => {
    if (!build || build.status !== "building" || !follow.current) return;
    const running = (build.steps || []).find((step) => step.status === "running");
    if (running && (!logStep || logStep.id !== running.id)) openLogs(running, { auto: true });
  }, [build, logStep, openLogs]);

  // A failed build lands on the output that matters, not on whichever step ran first.
  useEffect(() => {
    if (!build || failureOpened.current) return;
    if (build.status !== "failed" && build.status !== "cancelled") return;
    const point = failurePoint(build);
    if (!point) return;
    failureOpened.current = true;
    openLogs(point.step, { auto: true });
  }, [build, openLogs]);

  // When the viewed step stops running, fetch once more so the panel shows the
  // final output instead of a mid-stream tail.
  useEffect(() => {
    if (!logStep || !build) return;
    const step = (build.steps || []).find((item) => item.id === logStep.id);
    const status = step?.status || null;
    if (previousViewedStatus.current === "running" && status && status !== "running") {
      fetchLog(logStep);
    }
    previousViewedStatus.current = status;
  }, [build, logStep, fetchLog]);

  useEffect(() => {
    if (!logStep || !build || build.status !== "building") return undefined;
    const step = (build.steps || []).find((item) => item.id === logStep.id);
    if (!step || step.status !== "running") return undefined;
    const id = setInterval(() => fetchLog(logStep), LOG_REFRESH_MS);
    return () => clearInterval(id);
  }, [logStep, build, fetchLog]);

  useEffect(() => {
    const element = logEl.current;
    if (element && stickToBottom.current) element.scrollTop = element.scrollHeight;
  }, [logs]);

  const onLogScroll = () => {
    const element = logEl.current;
    if (!element) return;
    stickToBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
  };

  const plan = useMemo(() => (build ? buildBlueprint(build) : null), [build]);
  const progress = useMemo(() => (build ? buildProgress(build) : null), [build]);
  const point = useMemo(
    () => (build && (build.status === "failed" || build.status === "cancelled")
      ? failurePoint(build)
      : null),
    [build]
  );

  if (!build) return <div className="card sg-cb-card"><p className="muted">Loading…</p></div>;

  const nodeById = Object.fromEntries((build.nodes || []).map((node) => [node.id, node]));
  const steps = build.steps || [];
  const viewedStep = logStep ? steps.find((step) => step.id === logStep.id) : null;
  const logIsLive = viewedStep?.status === "running";
  // The lane switcher shows everyone working on the phase you are reading.
  const laneSteps = logStep ? steps.filter((step) => step.phase === logStep.phase) : [];
  const started = parseApiTime(runStartedAt(build));
  const elapsed = Number.isFinite(started) ? now - started : null;
  const isGrowthRun = isGrowing(build);
  const duration = buildDuration(build);
  const isDone = build.status === "completed";
  const buildProfile = buildProfiles.find(
    (profile) => String(profile.id) === String(build.buildProfileId)
  );
  const ahead = progress
    ? progress.timeline.filter((cell) => cell.state === "todo")
    : [];
  const addonStep = steps.find((step) => step.phase === "addons");

  const act = async (fn, after) => {
    setBusy(true);
    try {
      await fn();
      if (after) after();
      else await load();
    } catch (error) {
      notify(error.message, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sg-cb-detail">
      <div className="sg-cb-detail-head">
        <button className="btn-ghost" type="button" onClick={onBack}>← All builds</button>
        <h3>{build.name}</h3>
        {build.status === "building"
          ? <LiveBadge label={isGrowthRun ? "Adding machines" : "Building"} />
          : <StatusPill status={build.status} />}
        <span className="muted sg-cb-mono sg-cb-detail-meta">
          v{build.k8sVersion} · {build.topologyType === "stacked_ha" ? "HA" : "single CP"}
          {" "}· {build.controlPlaneEndpoint} · {build.cniPlugin}
        </span>
        <span className="sg-cb-detail-actions">
          {isRunning && elapsed !== null ? (
            <span className="muted sg-cb-detail-el">
              elapsed <b className="sg-cb-mono">{formatClock(elapsed)}</b>
              {progress?.position ? ` · phase ${progress.position} of ${progress.total}` : ""}
            </span>
          ) : null}
          {canExecute && build.status === "building" ? (
            <button
              className="btn-outline"
              type="button"
              disabled={busy}
              onClick={() => act(() => cancelClusterBuild(build.id))}
            >
              Cancel build
            </button>
          ) : null}
          {build.status !== "building" ? (
            <button
              className="btn-danger"
              type="button"
              disabled={busy}
              onClick={() => act(() => deleteClusterBuild(build.id), onDeleted)}
            >
              Delete
            </button>
          ) : null}
        </span>
      </div>

      {build.error && !point ? <ErrorBanner message={build.error} /> : null}

      {progress?.timeline?.length ? (
        <div className="card sg-cb-railcard">
          <PhaseRail
            timeline={progress.timeline}
            activePhase={logStep?.phase}
            onSelectPhase={(cell) => {
              const running = cell.steps.find((step) => step.status === "running");
              openLogs(running || cell.steps[cell.steps.length - 1]);
            }}
          />
          {isRunning && progress.current ? (
            <div className="sg-cb-railnow">
              <b>{PHASE_LABELS[progress.current.phase] || progress.current.phase}</b>
              {PHASE_NOTES[progress.current.phase]
                ? <span className="muted">{PHASE_NOTES[progress.current.phase]}</span>
                : null}
              <div
                className="sg-cb-progressbar"
                role="progressbar"
                aria-valuenow={progress.percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`${progress.done} of ${progress.total} phases complete`}
              >
                <i style={{ width: `${Math.max(progress.percent, 2)}%` }} />
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {point ? (
        <FailureHero
          point={point}
          build={build}
          log={logs}
          canExecute={canExecute}
          busy={busy}
          onRetry={() => act(() => retryClusterBuild(build.id), () => {
            failureOpened.current = false;
            follow.current = true;
            load();
          })}
        />
      ) : null}

      {isDone ? (
        <Receipt
          build={build}
          catalog={addonCatalog}
          duration={duration}
          addonStep={addonStep}
          onOpenAddonLog={() => addonStep && openLogs(addonStep)}
          dayTwo={(
            <DayTwo
              build={build}
              canCreate={canCreate}
              canDownloadKubeconfig={canDownloadKubeconfig}
              onOpenCluster={onOpenCluster}
              onGrow={() => setGrowing(true)}
              notify={notify}
              busy={busy}
              setBusy={setBusy}
            />
          )}
        />
      ) : null}

      {isDone && growing ? (
        <GrowPanel
          build={build}
          canExecute={canExecute}
          notify={notify}
          onChanged={load}
          onClose={() => setGrowing(false)}
        />
      ) : null}

      <div className="sg-cb-split">
        <div className="sg-cb-vstack">
          {logs ? (
            <div className="card sg-cb-card sg-cb-well">
              <div className="sg-cb-well-head">
                <h3>{PHASE_LABELS[logs.phase] || logs.phase}</h3>
                {logIsLive ? <LiveBadge label="live" /> : null}
                {laneSteps.length > 1 ? (
                  <div className="sg-cb-lanes">
                    {laneSteps.map((step) => {
                      const state = step.status === "completed" ? "is-done"
                        : step.status === "running" ? "is-now"
                          : step.status === "failed" ? "is-fail" : "";
                      return (
                        <button
                          key={step.id}
                          type="button"
                          className={`sg-cb-lane ${state} ${logStep?.id === step.id ? "is-open" : ""}`}
                          onClick={() => openLogs(step)}
                        >
                          <i />{stepLabel(step, nodeById)}
                          {step.attempt > 1 ? <span className="sg-cb-attempt">×{step.attempt}</span> : null}
                        </button>
                      );
                    })}
                  </div>
                ) : null}
                <span className="muted sg-cb-well-note">
                  secrets scrubbed before anything is stored
                </span>
              </div>
              {/* The failure hero already prints this step's error above; showing
                  it again here is the same paragraph twice on one screen. */}
              {logs.error && logs.id !== point?.step?.id
                ? <ErrorBanner message={logs.error} />
                : null}
              <pre className="sg-cb-log" ref={logEl} onScroll={onLogScroll}>
                {logs.logTail || (logIsLive ? "(waiting for output…)" : "(empty)")}
              </pre>
            </div>
          ) : null}

          {isRunning && ahead.length ? (
            <div className="card sg-cb-card">
              <div className="sg-cb-sect">
                <h2>Still ahead</h2>
                <span className="sg-cb-sect-right">
                  {ahead.length} phase{ahead.length === 1 ? "" : "s"}
                </span>
              </div>
              <ul className="sg-cb-proofs">
                {ahead.map((cell) => (
                  <li className="sg-cb-proof is-todo" key={cell.phase}>
                    <span className="tick" aria-hidden="true">
                      {progress.timeline.indexOf(cell) + 1}
                    </span>
                    <span className="what">{PHASE_LABELS[cell.phase] || cell.phase}</span>
                    <span className="how">{PHASE_NOTES[cell.phase] || ""}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <details className="sg-cb-machines">
            <summary>
              All machines
              <span className="muted">
                {(build.nodes || []).length} · addresses, guest OS and placement
              </span>
              <span className="sg-cb-chev" aria-hidden="true">›</span>
            </summary>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Machine</th><th>Role</th><th>Address</th>
                    <th>Guest OS</th><th>ESXi host</th><th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {(build.nodes || []).map((node) => (
                    <tr key={node.id}>
                      <td>
                        {node.hostname || node.vsphereVmName || "—"}
                        {node.isPrimaryCp ? " ★" : ""}
                        {node.isLbMaster ? " (VRRP master)" : ""}
                      </td>
                      <td>{ROLE_LABELS[node.role]}</td>
                      <td className="sg-cb-mono">
                        {node.address}{node.addressSource === "manual" ? " (manual)" : ""}
                      </td>
                      <td>{node.osFamily ? `${node.osFamily} ${node.osVersion || ""}` : "—"}</td>
                      <td>{node.vsphereHost || "—"}</td>
                      <td><StatusPill status={node.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </div>

        <Blueprint
          plan={plan}
          facts={[
            {
              label: "Add-ons",
              value: (build.addons || []).length
                ? (build.addons || [])
                  .map((addon) => addonDisplayName(addon, addonCatalog))
                  .join(", ")
                : "none",
            },
            { label: "Sources", value: buildProfile?.name || "Internet defaults" },
            {
              label: "Images",
              value: buildProfile?.k8sImageRegistry || "upstream registries",
            },
          ]}
          note={isDone
            ? { tone: "good", text: "Every HA tier spans a distinct ESXi host where vCenter reported one." }
            : point
              ? {
                tone: "bad",
                text: `Retry resets only what failed. Everything already joined stays joined — `
                  + `the build resumes at ${point.phaseLabel.toLowerCase()}.`,
              }
              : isRunning
                ? {
                  tone: "plain",
                  text: "Control planes join one at a time, with etcd quorum re-checked between "
                    + "each — which is why that phase is the slow one.",
                }
                : null}
        />
      </div>

      {(build.addons || []).length && !isDone ? (
        <div className="card sg-cb-addonline">
          <span className="sg-cb-config-label">Add-ons queued</span>
          <AddonChips addons={build.addons} catalog={addonCatalog} />
        </div>
      ) : null}
    </div>
  );
}
