/** History — every upgrade, and the report of the one you pick. */

import { useEffect, useMemo, useState } from "react";
import { getUpgradeJob, listUpgradeJobs } from "../../api/upgradesApi.js";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { MODE_LABEL, StatusChip, duration, formatWhen, ms, shortDuration } from "./upgradeUi.jsx";

function NodeTimes({ job }) {
  const nodes = (job.nodes || []).filter((n) => n.startedAt);
  const start = ms(job.startedAt);
  const end = ms(job.finishedAt) || Date.now();
  if (!nodes.length || start === null || end <= start) return <p className="uc-sub">No per-node timing for this run.</p>;
  const span = end - start;
  return (
    <div className="uc-gantt" role="table" aria-label="Time per node">
      <div className="uc-gantt__axis uc-sub" role="row">
        <span role="columnheader">Node</span>
        <span role="columnheader">0 → {shortDuration(job.startedAt, job.finishedAt)}</span>
        <span role="columnheader" className="uc-right">Took</span>
      </div>
      {nodes.map((n) => {
        const a = ms(n.startedAt);
        const b = ms(n.finishedAt) || end;
        const left = ((a - start) / span) * 100;
        const width = Math.max(1, ((b - a) / span) * 100);
        return (
          <div key={n.name} className="uc-gantt__row" role="row">
            <span className="uc-mono" role="cell">{n.name}</span>
            <span className="uc-gantt__track" role="cell">
              <span className={`uc-gantt__bar${n.status === "failed" ? " is-failed" : ""}`}
                style={{ left: `${left}%`, width: `${width}%` }} title={`${n.name}: ${shortDuration(n.startedAt, n.finishedAt)}`} />
            </span>
            <span className="uc-mono uc-sub uc-right" role="cell">
              {n.status === "failed" ? "failed" : n.finishedAt ? shortDuration(n.startedAt, n.finishedAt) : "running"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function Report({ job, onOpenCluster }) {
  const nodes = job.nodes || [];
  const done = nodes.filter((n) => n.status === "done").length;
  const votes = job.approvals?.votes || [];
  const story = job.status === "failed" ? job.error || job.message : job.message;
  return (
    <section className="uc-card uc-report" aria-labelledby="uc-report-h">
      <div className="uc-run__head">
        <div className="uc-stack uc-gap-6">
          <span className="uc-eyebrow">Run report · {MODE_LABEL[job.mode] || "Automatic"}</span>
          <h2 id="uc-report-h" className="uc-h2">
            {job.clusterName || job.clusterId} <span className="uc-mono uc-sub">{job.currentVersion || job.versionBefore} → {job.targetVersion}</span>
          </h2>
          <span className="uc-sub">
            {formatWhen(job.startedAt || job.requestedAt, { withYear: true })}
            {job.requestedBy?.name ? ` · requested by ${job.requestedBy.name}` : ""}
            {votes.length ? ` · ${votes.map((v) => `${v.decision === "approve" ? "approved" : "rejected"} by ${v.name}`).join(", ")}` : ""}
          </span>
        </div>
        <button type="button" className="btn-outline uc-btn-sm" onClick={() => onOpenCluster(job.clusterId, { job: job.jobId })}>
          Open in the cluster
        </button>
      </div>
      <div className={`uc-callout ${job.status === "completed" ? (job.verdict === "warning" ? "uc-callout--warn" : "uc-callout--ok") : job.status === "failed" ? "uc-callout--danger" : ""}`}>
        <StatusChip status={job.status} />
        <span className="uc-pre">{story || "—"}</span>
      </div>
      {job.startedAt ? (
      <div className="uc-stats uc-stats--4">
        <div><b>{job.finishedAt || job.startedAt ? duration(job.startedAt, job.finishedAt) || "—" : "—"}</b><span className="uc-sub">{job.finishedAt ? "total time" : "so far"}</span></div>
        <div><b>{nodes.length ? `${done} / ${nodes.length}` : "—"}</b><span className="uc-sub">nodes upgraded</span></div>
        <div><b>{job.baseline?.readyPods ?? "—"}</b><span className="uc-sub">ready pods before</span></div>
        <div><b>{job.after?.readyPods ?? "—"}</b><span className="uc-sub">ready pods after</span></div>
      </div>
      ) : null}
      {nodes.length ? (
        <div className="uc-stack uc-gap-8">
          <h3 className="uc-h3">Time per node</h3>
          <NodeTimes job={job} />
        </div>
      ) : null}
      <dl className="uc-kv">
        <dt>API server</dt>
        <dd className="uc-mono">{job.versionBefore || job.currentVersion} → {job.versionAfter || "—"}</dd>
        {job.snapshot ? (
          <>
            <dt>etcd snapshot</dt>
            <dd>{job.snapshot.status === "done" ? job.snapshot.message : `${job.snapshot.status}: ${job.snapshot.message || ""}`}</dd>
          </>
        ) : null}
        {job.options ? (
          <>
            <dt>Ran with</dt>
            <dd>
              {job.options.waveSize} worker{job.options.waveSize === 1 ? "" : "s"} at a time · drain {job.options.drainTimeoutMinutes} min
            </dd>
          </>
        ) : null}
        {job.waivers?.length ? (
          <>
            <dt>Waived blockers</dt>
            <dd>{job.waivers.map((w) => `${w.name} (${w.reason})`).join("; ")}</dd>
          </>
        ) : null}
      </dl>
      {job.log?.length ? (
        <details className="uc-details">
          <summary>Run log · {job.log.length} lines</summary>
          <div className="uc-log uc-log--static">
            {job.log.map((line, i) => (
              <div key={i} className="uc-log__line">
                <span className="uc-sub">{new Date(line.at).toLocaleTimeString(undefined, { hour12: false })}</span>
                <span className="uc-log__node">{line.node || "run"}</span>
                <span>{line.message}</span>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

export default function HistoryView({ onOpenCluster }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    let cancelled = false;
    listUpgradeJobs({ limit: 100 })
      .then((data) => {
        if (cancelled) return;
        setItems(data.items || []);
        setSelected((current) => current || (data.items?.[0]?.jobId ?? null));
      })
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selected) return undefined;
    let cancelled = false;
    setDetail(null);
    getUpgradeJob(selected)
      .then((data) => !cancelled && setDetail(data))
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const counts = useMemo(() => {
    const out = { completed: 0, failed: 0 };
    (items || []).forEach((j) => {
      if (j.status in out) out[j.status] += 1;
    });
    return out;
  }, [items]);

  if (error && !items) return <ErrorBanner message={error} />;
  if (!items) return <div className="uc-loading">Loading history…</div>;
  if (!items.length) {
    return (
      <section className="uc-card uc-empty-state">
        <h2 className="uc-h2">No upgrades yet</h2>
        <p className="uc-muted">Every upgrade KubeSight runs or tracks, its approvals and its report land here.</p>
      </section>
    );
  }

  return (
    <div className="uc-two uc-history">
      <section className="uc-card uc-runs" aria-labelledby="uc-runs-h">
        <div className="uc-card__head">
          <h2 id="uc-runs-h" className="uc-h3">Runs</h2>
          <span className="uc-sub">{counts.completed} completed · {counts.failed} failed</span>
        </div>
        <ol className="uc-runs__list">
          {items.map((job) => (
            <li key={job.jobId}>
              <button type="button" className={`uc-runrow${selected === job.jobId ? " is-on" : ""}`}
                aria-pressed={selected === job.jobId} onClick={() => setSelected(job.jobId)}>
                <span className="uc-stack">
                  <span className="uc-strong">
                    {job.clusterName || job.clusterId}{" "}
                    <span className="uc-mono uc-sub">{job.currentVersion || job.versionBefore} → {job.targetVersion}</span>
                  </span>
                  <span className="uc-sub">
                    {formatWhen(job.startedAt || job.requestedAt || job.createdAt)} · {MODE_LABEL[job.mode] || "Automatic"}
                    {job.finishedAt ? ` · ${duration(job.startedAt || job.requestedAt, job.finishedAt)}` : ""}
                  </span>
                </span>
                <StatusChip status={job.status} />
              </button>
            </li>
          ))}
        </ol>
      </section>
      <div className="uc-two__main">
        {detail ? <Report job={detail} onOpenCluster={onOpenCluster} /> : <div className="uc-loading">Loading the report…</div>}
        {error && items ? <ErrorBanner message={error} /> : null}
      </div>
    </div>
  );
}
