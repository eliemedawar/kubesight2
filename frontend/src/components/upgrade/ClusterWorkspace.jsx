/** One cluster: choose a target, check readiness, plan and approve, watch the run —
 *  or, when KubeSight does not run the upgrade itself, the guided runbook. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { useRouter } from "../../routes/RouterContext.jsx";
import { getUpgradeJob, getUpgradeWorkspace, runUpgradeReadiness } from "../../api/upgradesApi.js";
import { setVisibleInterval } from "../../lib/visibleInterval";
import PlanPanel from "./PlanPanel.jsx";
import ReadinessPanel from "./ReadinessPanel.jsx";
import RequestPanel from "./RequestPanel.jsx";
import RunView from "./RunView.jsx";
import RunbookView from "./RunbookView.jsx";
import {
  ACTIVE_STATUSES,
  Chip,
  IconBack,
  ModeChip,
  PENDING_STATUSES,
  SupportChip,
  formatDay,
} from "./upgradeUi.jsx";

const FINAL = ["completed", "failed", "stopped", "rejected", "withdrawn", "expired"];
// A request that ended without running: shown in the request panel, not as a run.
const ENDED_UNRUN = ["rejected", "withdrawn", "expired"];
const RUN_ENDED = ["completed", "failed", "stopped"];

function Flow({ steps }) {
  return (
    <ol className="uc-flow" aria-label="Upgrade steps">
      {steps.map((step, i) => (
        <li key={step.key} className={`uc-flow__step is-${step.state}`} aria-current={step.state === "cur" ? "step" : undefined}>
          <span className="uc-flow__n" aria-hidden="true">{step.state === "done" ? "✓" : i + 1}</span>
          <span>{step.label}</span>
          {step.state === "done" ? <span className="uc-sr"> (done)</span> : null}
        </li>
      ))}
    </ol>
  );
}

function TargetPanel({ ws, target, onTarget, locked }) {
  const hops = ws.path || [];
  const patch = ws.patchTarget && ws.patchTarget !== ws.version ? ws.patchTarget : null;
  return (
    <section className="uc-card" aria-labelledby="uc-target-h">
      <h3 id="uc-target-h" className="uc-h3">Where to</h3>
      <ol className="uc-path">
        <li className="uc-path__now">
          <span className="uc-path__dot" aria-hidden="true" />
          <span className="uc-mono uc-strong">{ws.version}</span>
          <span className="uc-sub">Running now</span>
        </li>
        {hops.map((hop, index) => {
          const selectable = index === 0 && hop.version && !locked;
          const selected = target === hop.version;
          return (
            <li key={hop.minor} className={`uc-path__hop${selected ? " is-selected" : ""}`}>
              <span className="uc-path__dot" aria-hidden="true" />
              {index === 0 ? (
                <label className={`uc-option${selected ? " is-selected" : ""}`}>
                  <input type="radio" name="uc-target" checked={selected} disabled={!selectable}
                    onChange={() => onTarget(hop.version)} />
                  <span className="uc-stack">
                    <span className="uc-option__top">
                      <span className="uc-mono uc-strong">{hop.label}</span>
                      <SupportChip support={{ state: "supported", endOfLife: hop.endOfLife, estimated: hop.estimated }} />
                    </span>
                    <span className="uc-sub">Next minor, newest patch{hop.version ? "" : " (not known yet)"}</span>
                  </span>
                </label>
              ) : (
                <>
                  <span className="uc-mono">{hop.label}</span>
                  <span className="uc-sub">Hop {hop.hop}{hop.latest ? " · latest" : ""} · its own run</span>
                </>
              )}
            </li>
          );
        })}
      </ol>
      {hops.length > 1 ? (
        <p className="uc-sub">
          kubeadm moves one minor version per run, so reaching {hops[hops.length - 1].minor} takes {hops.length} runs.
          Each gets its own readiness check and approval.
        </p>
      ) : null}
      {!hops.length ? <p className="uc-sub">This cluster is on the newest minor version.</p> : null}
      {patch ? (
        <label className={`uc-option${target === patch ? " is-selected" : ""}`}>
          <input type="radio" name="uc-target" checked={target === patch} disabled={locked} onChange={() => onTarget(patch)} />
          <span className="uc-stack">
            <span className="uc-mono uc-strong">{patch} <span className="uc-sub">patch only</span></span>
            <span className="uc-sub">
              Bug and security fixes{ws.support?.state !== "supported" ? `; support still ends ${formatDay(ws.support?.endOfLife)}` : ""}.
            </span>
          </span>
        </label>
      ) : null}
    </section>
  );
}

function VersionsCard({ ws }) {
  const versions = {};
  (ws.nodes || []).forEach((n) => {
    versions[n.version] = (versions[n.version] || 0) + 1;
  });
  return (
    <section className="uc-card" aria-labelledby="uc-vers-h">
      <h3 id="uc-vers-h" className="uc-h3">On the cluster</h3>
      <dl className="uc-kv">
        <dt>API server</dt>
        <dd className="uc-mono">{ws.version}</dd>
        <dt>kubelets</dt>
        <dd className="uc-mono">
          {Object.entries(versions).map(([v, n]) => `${v} on ${n}`).join(", ") || "—"}
        </dd>
        <dt>Control planes</dt>
        <dd>{(ws.nodes || []).filter((n) => n.role === "control-plane").map((n) => n.name).join(", ") || "—"}</dd>
        <dt>Workers</dt>
        <dd>{ws.workers}</dd>
        {ws.pools?.length ? (
          <>
            <dt>Node pools</dt>
            <dd>{ws.pools.join(", ")}</dd>
          </>
        ) : null}
      </dl>
    </section>
  );
}

export default function ClusterWorkspace({ clusterId, focusJobId, canStart, isAdmin, onBack }) {
  const { navigate } = useRouter();
  const [ws, setWs] = useState(null);
  const [error, setError] = useState("");
  const [target, setTarget] = useState(null);
  const [readiness, setReadiness] = useState(null);
  const [checking, setChecking] = useState(false);
  const [stage, setStage] = useState("readiness");
  const [waivers, setWaivers] = useState({});
  const [job, setJob] = useState(null);
  // The run this page watched: it stays on screen after it ends (the cluster
  // then has no open job) until "Plan the next upgrade".
  const [watchedJobId, setWatchedJobId] = useState(null);
  const autoChecked = useRef(new Set());

  const loadWorkspace = useCallback(async () => {
    try {
      const data = await getUpgradeWorkspace(clusterId);
      setWs(data);
      setError("");
      setTarget((current) => {
        if (current) return current;
        if (data.openJob?.targetVersion) return data.openJob.targetVersion;
        const hop = (data.path || [])[0];
        if (hop?.version) return hop.version;
        if (data.patchTarget && data.patchTarget !== data.version) return data.patchTarget;
        return hop ? `v${hop.minor}.0` : null;
      });
      return data;
    } catch (err) {
      setError(err.message);
      return null;
    }
  }, [clusterId]);

  useEffect(() => {
    loadWorkspace();
  }, [loadWorkspace]);

  // The stored readiness result, when it is for the chosen target.
  useEffect(() => {
    if (!ws || !target) return;
    setReadiness((current) => {
      if (current && current.targetVersion === target) return current;
      return ws.readiness && ws.readiness.targetVersion === target ? ws.readiness : current;
    });
  }, [ws, target]);

  const jobId = focusJobId || ws?.openJob?.jobId || watchedJobId || null;
  useEffect(() => {
    const open = ws?.openJob;
    if (open?.jobId && open.mode !== "guided" && ACTIVE_STATUSES.includes(open.status)) setWatchedJobId(open.jobId);
  }, [ws?.openJob]);
  const loadJob = useCallback(async () => {
    if (!jobId) {
      setJob(null);
      return null;
    }
    try {
      const data = await getUpgradeJob(jobId);
      setJob((previous) => {
        if (previous && !FINAL.includes(previous.status) && FINAL.includes(data.status)) {
          // A run just ended: the cluster's versions changed.
          loadWorkspace();
        }
        return data;
      });
      return data;
    } catch (err) {
      setError(err.message);
      return null;
    }
  }, [jobId, loadWorkspace]);

  const jobStatus = job?.status;
  useEffect(() => {
    if (!jobId) {
      setJob(null);
      return undefined;
    }
    loadJob();
    if (jobStatus && FINAL.includes(jobStatus)) return undefined;
    const every = jobStatus && PENDING_STATUSES.includes(jobStatus) ? 10000 : 2000;
    return setVisibleInterval(loadJob, every);
  }, [jobId, jobStatus, loadJob]);

  const runReadiness = useCallback(async () => {
    if (!target) return;
    setChecking(true);
    try {
      const data = await runUpgradeReadiness(clusterId, target);
      setReadiness(data);
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setChecking(false);
    }
  }, [clusterId, target]);

  const automatic = ws?.mode === "automatic";
  const showJob =
    job && job.mode !== "guided" &&
    (focusJobId || !FINAL.includes(job.status) || (watchedJobId === job.jobId && RUN_ENDED.includes(job.status)));
  const jobActive = showJob && (ACTIVE_STATUSES.includes(job.status) || RUN_ENDED.includes(job.status));
  const jobPending = showJob && (PENDING_STATUSES.includes(job.status) || ENDED_UNRUN.includes(job.status));

  // First visit for a target with no stored result: check once, automatically.
  useEffect(() => {
    if (!ws || !automatic || !target || showJob || checking) return;
    const key = `${clusterId}:${target}`;
    if (autoChecked.current.has(key)) return;
    if (ws.readiness && ws.readiness.targetVersion === target) return;
    if (readiness && readiness.targetVersion === target) return;
    autoChecked.current.add(key);
    runReadiness();
  }, [ws, automatic, target, showJob, checking, readiness, runReadiness, clusterId]);

  const flow = useMemo(() => {
    if (!ws) return [];
    if (!automatic) {
      const guidedJob = job?.mode === "guided" ? job : null;
      return [
        { key: "target", label: target ? `Target · ${target}` : "Target", state: "done" },
        { key: "runbook", label: ws.mode === "manual" ? "Upgrade from the platform" : "Runbook", state: guidedJob?.status === "completed" ? "done" : "cur" },
        { key: "verify", label: "Seen on the cluster", state: guidedJob?.status === "completed" ? "done" : "todo" },
      ];
    }
    const status = showJob ? job.status : null;
    const at = status
      ? PENDING_STATUSES.includes(status) || ENDED_UNRUN.includes(status) ? "plan" : status === "completed" ? "done" : "run"
      : stage;
    const order = ["target", "readiness", "plan", "run", "verify"];
    const idx = at === "done" ? 5 : order.indexOf(at);
    const labels = {
      target: target ? `Target · ${target}` : "Target",
      readiness: readiness?.summary ? `Readiness${readiness.summary.warning ? ` · ${readiness.summary.warning} warning${readiness.summary.warning === 1 ? "" : "s"}` : ""}` : "Readiness",
      plan: "Plan & approval",
      run: "Run",
      verify: "Verify",
    };
    return order.map((key, i) => ({ key, label: labels[key], state: i < idx ? "done" : i === idx ? "cur" : "todo" }));
  }, [ws, automatic, job, showJob, stage, target, readiness]);

  const onTarget = (version) => {
    setTarget(version);
    setStage("readiness");
    setWaivers({});
  };
  const planNext = async () => {
    setWatchedJobId(null);
    setJob(null);
    setTarget(null);
    setReadiness(null);
    setStage("readiness");
    if (focusJobId) navigate({ key: "upgradeCluster", params: { clusterId } }, { replace: true });
    await loadWorkspace();
  };
  const onSubmitted = async () => {
    setStage("readiness");
    setWaivers({});
    await loadWorkspace();
  };

  if (!ws) {
    return (
      <div className="uc-workspace">
        <button type="button" className="btn-ghost uc-back" onClick={onBack}><IconBack /> Upgrade Center</button>
        {error ? <ErrorBanner message={error} /> : <div className="uc-loading">Reading the cluster…</div>}
      </div>
    );
  }

  const staleFor = readiness && target && readiness.targetVersion !== target ? readiness.targetVersion : null;

  return (
    <div className="uc-workspace">
      <header className="uc-pagehead uc-pagehead--ws">
        <div className="uc-pagehead__title">
          <button type="button" className="btn-ghost uc-back" onClick={onBack}><IconBack /> Upgrade Center</button>
          <div className="uc-titleline">
            <h1 className="uc-display uc-h1">{ws.name}</h1>
            <Chip tone="muted">{ws.providerDisplay} · {ws.controlPlanes} control plane{ws.controlPlanes === 1 ? "" : "s"} · {ws.workers} worker{ws.workers === 1 ? "" : "s"}</Chip>
            <SupportChip support={ws.support} />
            <ModeChip mode={ws.mode} title={ws.modeReason} />
          </div>
        </div>
        <Flow steps={flow} />
      </header>

      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}

      {jobActive ? (
        <RunView job={job} canControl={canStart && job.canControl !== false} onChanged={loadJob}
          onPlanNext={canStart ? planNext : null} />
      ) : jobPending ? (
        <div className="uc-two">
          <div className="uc-two__main">
            <RequestPanel job={job} onChanged={async () => { await loadJob(); await loadWorkspace(); }}
              onPlanNext={canStart && ENDED_UNRUN.includes(job.status) ? planNext : null} />
          </div>
          <aside className="uc-two__side">
            <VersionsCard ws={ws} />
          </aside>
        </div>
      ) : !automatic ? (
        <RunbookView ws={ws} target={target} onTargetChange={setTarget} canStart={canStart} onChanged={loadWorkspace} />
      ) : stage === "plan" ? (
        <PlanPanel ws={ws} target={target} readiness={readiness} waivers={waivers}
          onBack={() => setStage("readiness")} onSubmitted={onSubmitted} />
      ) : (
        <div className="uc-two">
          <aside className="uc-two__side">
            <TargetPanel ws={ws} target={target} onTarget={onTarget} locked={checking} />
            <VersionsCard ws={ws} />
          </aside>
          <div className="uc-two__main">
            {target ? (
              <ReadinessPanel
                readiness={readiness}
                checking={checking}
                target={target}
                isAdmin={isAdmin}
                canStart={canStart}
                waivers={waivers}
                onWaiver={(key, reason) => setWaivers((w) => ({ ...w, [key]: reason }))}
                onRecheck={runReadiness}
                onContinue={() => setStage("plan")}
                staleFor={staleFor}
              />
            ) : (
              <section className="uc-card"><p className="uc-muted">Nothing to upgrade to: this cluster is on the newest release.</p></section>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
