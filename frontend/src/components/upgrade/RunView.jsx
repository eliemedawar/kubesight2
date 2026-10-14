/** A run, live and after: node by node, the log, the cluster against its baseline. */

import { useEffect, useRef, useState } from "react";
import { controlUpgrade } from "../../api/upgradesApi.js";
import { confirmDialog } from "../../lib/dialogs.js";
import {
  Chip,
  IconPause,
  IconPlay,
  IconStop,
  StatusChip,
  duration,
  formatWhen,
} from "./upgradeUi.jsx";

const PHASES = [
  ["cordon", "Cordon"],
  ["drain", "Drain"],
  ["upgrade", "Upgrade"],
  ["uncordon", "Uncordon"],
  ["ready", "Ready"],
];

const NODE_TONE = { done: "ok", active: "run", failed: "danger", skipped: "muted", pending: "muted" };

function NodeCard({ node }) {
  const state = node.status || "pending";
  const label =
    state === "done" ? node.message || `Ready on ${node.versionAfter}` :
    state === "skipped" ? "Already on the target" :
    state === "pending" ? (node.wave ? `Wave ${node.wave}` : "Waiting") :
    node.message || state;
  return (
    <li className={`uc-node uc-node--${state}`}>
      <div className="uc-node__head">
        <div className="uc-stack">
          <span className="uc-strong">{node.name}</span>
          <span className="uc-sub">
            {node.role === "control-plane" ? (node.first ? "Control plane · first" : "Control plane") : `Worker · wave ${node.wave}`}
            {node.pods ? ` · ${node.pods} pods` : ""}
          </span>
        </div>
        <span className="uc-mono uc-sub">{node.versionAfter || node.versionBefore}</span>
      </div>
      <ol className="uc-phases" aria-label={`${node.name} phases`}>
        {PHASES.map(([key, word]) => {
          const phase = node.phases?.[key] || "pending";
          return (
            <li key={key} className={`uc-phase uc-phase--${phase}`}>
              <i aria-hidden="true" />
              <span>{word}</span>
              <span className="uc-sr">: {phase}</span>
            </li>
          );
        })}
      </ol>
      <Chip tone={NODE_TONE[state] || "muted"} className="uc-chip--wrap">{label}</Chip>
    </li>
  );
}

function Track({ job }) {
  const stages = job.stages || [];
  const nodes = job.nodes || [];
  const safety = stages.find((s) => s.key === "safety");
  const verify = stages.find((s) => s.key === "verify");
  const seg = (status) => (status === "done" || status === "skipped" ? "done" : status === "active" ? "act" : status === "failed" ? "fail" : status === "warning" ? "done" : "");
  const items = [
    { key: "safety", label: "Safety net", st: seg(safety?.status) },
    ...nodes.map((n) => ({ key: n.name, label: n.name, st: seg(n.status) })),
    { key: "verify", label: "Verify", st: seg(verify?.status) },
  ];
  return (
    <ol className="uc-track" style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}>
      {items.map((item) => (
        <li key={item.key} className={`uc-track__seg ${item.st ? `is-${item.st}` : ""}`}>
          <i aria-hidden="true" />
          <span>{item.label}</span>
        </li>
      ))}
    </ol>
  );
}

function LiveLog({ lines }) {
  const box = useRef(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = box.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines.length]);
  return (
    <div
      ref={box}
      className="uc-log"
      role="log"
      aria-live="polite"
      tabIndex={0}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
    >
      {lines.length === 0 ? <span className="uc-sub">No log lines yet.</span> : null}
      {lines.map((line, i) => (
        <div key={i} className="uc-log__line">
          <span className="uc-sub">{new Date(line.at).toLocaleTimeString(undefined, { hour12: false })}</span>
          <span className="uc-log__node">{line.node || "run"}</span>
          <span>{line.message}</span>
        </div>
      ))}
    </div>
  );
}

export default function RunView({ job, canControl, onChanged, onPlanNext }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const nodes = job.nodes || [];
  const done = nodes.filter((n) => n.status === "done" || n.status === "skipped").length;
  const active = nodes.filter((n) => n.status === "active");
  const running = ["queued", "running", "paused"].includes(job.status);
  const finished = ["completed", "failed", "stopped"].includes(job.status);
  const baseline = job.baseline;
  const after = job.after;
  const onTarget = nodes.filter((n) => n.versionAfter && n.versionAfter === job.targetVersion).length;
  const failedNode = nodes.find((n) => n.status === "failed");
  const lines = job.log || [];

  const act = async (action) => {
    if (action === "stop") {
      const ok = await confirmDialog({
        title: "Stop this upgrade?",
        message:
          "KubeSight finishes the node it is on, then stops. Nodes already done keep the new version; the rest stay where they are, within the supported skew. You can start the same upgrade again later and it resumes.",
        confirmLabel: "Stop after this node",
        tone: "danger",
      });
      if (!ok) return;
    }
    setBusy(action);
    setError("");
    try {
      await controlUpgrade(job.jobId, action);
      onChanged?.();
    } catch (err) {
      setError(err.serverMessage || err.message);
    } finally {
      setBusy("");
    }
  };

  let headline = "";
  if (job.status === "queued") headline = "Starting…";
  else if (job.status === "running") headline = active.length ? `Node ${done + 1} of ${nodes.length} · ${active.map((n) => n.name).join(", ")}` : job.currentStage === "safety" ? "Taking the safety net" : job.currentStage === "verify" ? "Verifying" : "Running";
  else if (job.status === "paused") headline = job.message || "Paused";
  else if (job.status === "completed") headline = job.message || `Upgraded to ${job.targetVersion}.`;
  else if (job.status === "failed") headline = "The upgrade stopped on an error.";
  else if (job.status === "stopped") headline = job.message || "Stopped.";

  return (
    <div className="uc-run">
      <section className="uc-card" aria-labelledby="uc-run-h">
        <div className="uc-run__head">
          <div className="uc-stack uc-gap-6">
            <StatusChip status={job.status}>
              {job.status === "running" ? `Running · ${duration(job.startedAt)}` : undefined}
            </StatusChip>
            <h2 id="uc-run-h" className="uc-h2" aria-live="polite">{headline}</h2>
            <span className="uc-sub">
              {job.currentVersion || job.versionBefore} → {job.targetVersion}
              {job.requestedBy?.name ? ` · requested by ${job.requestedBy.name}` : ""}
              {job.approvals?.votes?.length ? ` · approved by ${job.approvals.votes.filter((v) => v.decision === "approve").map((v) => v.name).join(", ")}` : ""}
              {job.startedAt ? ` · started ${formatWhen(job.startedAt)}` : ""}
              {finished && job.finishedAt ? ` · took ${duration(job.startedAt, job.finishedAt)}` : ""}
            </span>
            {job.controlMessage && running ? <span className="uc-note">{job.controlMessage}</span> : null}
          </div>
          {canControl && running ? (
            <div className="uc-row">
              {job.status === "paused" ? (
                <button type="button" className="btn-primary uc-btn-sm" onClick={() => act("resume")} disabled={!!busy}>
                  <IconPlay /> Resume
                </button>
              ) : (
                <button type="button" className="btn-outline uc-btn-sm" onClick={() => act("pause")}
                  disabled={!!busy || job.control?.pause}>
                  <IconPause /> {job.control?.pause ? "Pausing after this node…" : "Pause after this node"}
                </button>
              )}
              <button type="button" className="btn-danger-outline uc-btn-sm" onClick={() => act("stop")}
                disabled={!!busy || job.control?.stop}>
                <IconStop /> {job.control?.stop ? "Stopping…" : "Stop"}
              </button>
            </div>
          ) : null}
        </div>
        {error ? <p className="uc-error" role="alert">{error}</p> : null}
        {nodes.length ? <Track job={job} /> : null}
        {job.status === "failed" ? (
          <div className="uc-callout uc-callout--danger" role="alert">
            <strong>{failedNode ? `${failedNode.name}: ${failedNode.message}` : "What went wrong"}</strong>
            <span className="uc-pre">{job.error || job.message}</span>
            {job.cordonedNodes?.length ? (
              <span>Left cordoned for inspection: {job.cordonedNodes.join(", ")}. Uncordon them once fixed, or start the upgrade again to resume.</span>
            ) : (
              <span>Start the same upgrade again once fixed: nodes already on the target are skipped.</span>
            )}
          </div>
        ) : null}
        {job.status === "completed" ? (
          <div className={`uc-callout ${job.verdict === "warning" ? "uc-callout--warn" : "uc-callout--ok"}`}>
            <strong>{job.verdict === "warning" ? "Upgraded, with something to look at" : "Clean run"}</strong>
            <span>{job.message}</span>
          </div>
        ) : null}
        {finished && onPlanNext ? (
          <div className="uc-row">
            <button type="button" className="btn-outline uc-btn-sm" onClick={onPlanNext}>
              Plan the next upgrade
            </button>
          </div>
        ) : null}
      </section>

      <div className="uc-run__grid">
        <section className="uc-run__nodes" aria-labelledby="uc-nodes-h">
          <div className="uc-section-head">
            <h3 id="uc-nodes-h" className="uc-h3">Nodes</h3>
            <span className="uc-sub">Control planes first, then workers {job.options?.waveSize > 1 ? `${job.options.waveSize} at a time` : "one at a time"}</span>
          </div>
          <ul className="uc-nodes">
            {nodes.map((node) => (
              <NodeCard key={node.name} node={node} />
            ))}
          </ul>
        </section>

        <aside className="uc-run__side">
          <section className="uc-card" aria-labelledby="uc-now-h">
            <h3 id="uc-now-h" className="uc-h3">{finished ? "Before and after" : "Cluster right now"}</h3>
            <div className="uc-split-head">
              <span>kubelets</span>
              <span className="uc-mono">{onTarget} on {job.targetVersion} · {nodes.length - onTarget} on {job.versionBefore || job.currentVersion}</span>
            </div>
            <div className="uc-split" role="img" aria-label={`${onTarget} of ${nodes.length} kubelets upgraded`}>
              <span className="is-done" style={{ flex: onTarget }} />
              <span style={{ flex: Math.max(0, nodes.length - onTarget) }} />
            </div>
            <div className="uc-stats">
              <div>
                <b>{baseline?.readyPods ?? "—"}</b>
                <span className="uc-sub">ready pods before (baseline)</span>
              </div>
              {after ? (
                <div>
                  <b>{after.readyPods}</b>
                  <span className="uc-sub">ready pods after</span>
                </div>
              ) : null}
            </div>
            {job.snapshot ? (
              <div className="uc-snapshot">
                <Chip tone={job.snapshot.status === "done" ? "ok" : job.snapshot.status === "failed" ? "danger" : "muted"}>
                  etcd snapshot · {job.snapshot.status}
                </Chip>
                <span className="uc-sub">{job.snapshot.message}</span>
                {job.snapshot.restore ? (
                  <details>
                    <summary className="uc-sub">How to restore</summary>
                    <span className="uc-sub">{job.snapshot.restore}</span>
                  </details>
                ) : null}
              </div>
            ) : null}
          </section>

          <section className="uc-card" aria-labelledby="uc-log-h">
            <div className="uc-card__head">
              <h3 id="uc-log-h" className="uc-h3">Run log</h3>
              <span className="uc-sub">{lines.length} line{lines.length === 1 ? "" : "s"}</span>
            </div>
            <LiveLog lines={lines} />
          </section>
        </aside>
      </div>
    </div>
  );
}
