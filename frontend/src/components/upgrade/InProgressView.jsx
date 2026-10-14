/** In progress — every upgrade that is running, waiting for approval, scheduled or guided. */

import { StatusChip, duration, formatWhen, timeAgo } from "./upgradeUi.jsx";

const GROUPS = [
  { key: "running", label: "Running", statuses: ["queued", "running", "paused"] },
  { key: "approval", label: "Waiting for approval", statuses: ["awaiting_approval"] },
  { key: "scheduled", label: "Scheduled", statuses: ["scheduled"] },
  { key: "guided", label: "Guided, being tracked", statuses: ["guided"] },
];

function detail(job) {
  if (["queued", "running", "paused"].includes(job.status)) {
    const where = job.activeNode ? `${job.activeNode} · ` : "";
    return `${where}${job.nodesDone} of ${job.nodesTotal || "?"} nodes · ${duration(job.startedAt)}`;
  }
  if (job.status === "awaiting_approval") {
    const a = job.approvals || {};
    return `${a.approved || 0} of ${a.required || 0} approvals · asked by ${job.requestedBy || "someone"} ${timeAgo(job.requestedAt)}`;
  }
  if (job.status === "scheduled") {
    return job.schedule?.mode === "window"
      ? `Window ${formatWhen(job.schedule.start)}${job.schedule.end ? ` – ${formatWhen(job.schedule.end)}` : ""}`
      : "Starting";
  }
  return `Started ${timeAgo(job.startedAt)}`;
}

export default function InProgressView({ fleet, loading, onOpenCluster }) {
  const rows = (fleet?.clusters || []).filter((row) => row.openJob);
  if (loading) return <div className="uc-loading">Loading…</div>;
  if (!rows.length) {
    return (
      <section className="uc-card uc-empty-state">
        <h2 className="uc-h2">Nothing in progress</h2>
        <p className="uc-muted">Upgrades that are running, waiting for approval or scheduled for a window show here.</p>
      </section>
    );
  }
  return (
    <div className="uc-progress">
      {GROUPS.map((group) => {
        const items = rows.filter((row) => group.statuses.includes(row.openJob.status));
        if (!items.length) return null;
        return (
          <section key={group.key} className="uc-progress__group" aria-labelledby={`uc-pg-${group.key}`}>
            <h2 id={`uc-pg-${group.key}`} className="uc-eyebrow">{group.label} · {items.length}</h2>
            <ul className="uc-progress__list">
              {items.map((row) => (
                <li key={row.id}>
                  <button type="button" className="uc-progress__item" onClick={() => onOpenCluster(row.id)}>
                    <span className="uc-stack">
                      <span className="uc-strong">{row.name}</span>
                      <span className="uc-mono uc-sub">{row.openJob.currentVersion || row.version} → {row.openJob.targetVersion}</span>
                    </span>
                    <span className="uc-sub uc-progress__detail">{detail(row.openJob)}</span>
                    <StatusChip status={row.openJob.status} />
                  </button>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
