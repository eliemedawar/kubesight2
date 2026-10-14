/** A request waiting for its approvals or its window. */

import { useState } from "react";
import { approveUpgrade, rejectUpgrade, withdrawUpgrade } from "../../api/upgradesApi.js";
import { confirmDialog, promptDialog } from "../../lib/dialogs.js";
import { Chip, StatusChip, formatWhen, readinessWords, timeAgo } from "./upgradeUi.jsx";

export default function RequestPanel({ job, onChanged, onPlanNext }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const approvals = job.approvals || { required: 0, votes: [] };
  const approved = (approvals.votes || []).filter((v) => v.decision === "approve").length;
  const options = job.options || {};
  const sched = job.schedule || { mode: "asap" };

  const run = async (fn) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      onChanged?.();
    } catch (err) {
      setError(err.serverMessage || err.message);
    } finally {
      setBusy(false);
    }
  };

  const approve = async () => {
    const note = await promptDialog({
      title: `Approve upgrading ${job.clusterName} to ${job.targetVersion}?`,
      message: sched.mode === "window" ? `It starts when the window opens (${formatWhen(sched.start)}).` : "It starts as soon as the last approval is in.",
      label: "Comment (optional)",
      confirmLabel: "Approve",
    });
    if (note === null) return;
    run(() => approveUpgrade(job.jobId, note));
  };
  const reject = async () => {
    const note = await promptDialog({
      title: "Reject this upgrade",
      label: "Why",
      required: true,
      confirmLabel: "Reject",
      tone: "danger",
    });
    if (note === null) return;
    run(() => rejectUpgrade(job.jobId, note));
  };
  const withdraw = async () => {
    const ok = await confirmDialog({
      title: "Withdraw this request?",
      message: "Nothing has run yet. You can plan it again later.",
      confirmLabel: "Withdraw",
      tone: "danger",
    });
    if (ok) run(() => withdrawUpgrade(job.jobId));
  };

  return (
    <section className="uc-card uc-request" aria-labelledby="uc-req-h">
      <div className="uc-run__head">
        <div className="uc-stack uc-gap-6">
          <StatusChip status={job.status} />
          <h2 id="uc-req-h" className="uc-h2">
            {["rejected", "withdrawn", "expired"].includes(job.status)
              ? job.message
              : job.status === "awaiting_approval"
              ? `Waiting for ${approvals.required - approved} more approval${approvals.required - approved === 1 ? "" : "s"}`
              : sched.mode === "window"
                ? `Approved · starts ${formatWhen(sched.start)}`
                : "Approved · starting"}
          </h2>
          <span className="uc-sub">
            {job.currentVersion} → {job.targetVersion} · requested by {job.requestedBy?.name} {timeAgo(job.requestedAt)}
          </span>
        </div>
        <div className="uc-row">
          {job.canDecide ? (
            <>
              <button type="button" className="btn-outline uc-btn-sm" onClick={reject} disabled={busy}>Reject</button>
              <button type="button" className="btn-primary uc-btn-sm" onClick={approve} disabled={busy}>Approve</button>
            </>
          ) : null}
          {job.canWithdraw ? (
            <button type="button" className="btn-outline uc-btn-sm" onClick={withdraw} disabled={busy}>Withdraw</button>
          ) : null}
          {onPlanNext ? (
            <button type="button" className="btn-outline uc-btn-sm" onClick={onPlanNext}>Plan an upgrade</button>
          ) : null}
        </div>
      </div>
      {job.whyNot && job.status === "awaiting_approval" && !job.canWithdraw ? <p className="uc-note">{job.whyNot}</p> : null}
      {job.status === "awaiting_approval" && job.canWithdraw ? (
        <p className="uc-note">Someone else with the Start upgrades permission approves it, from here or from the Approvals inbox.</p>
      ) : null}
      {error ? <p className="uc-error" role="alert">{error}</p> : null}

      <dl className="uc-kv">
        <dt>Workers at a time</dt>
        <dd>{options.waveSize || 1}</dd>
        <dt>Drain timeout</dt>
        <dd>{options.drainTimeoutMinutes || 10} min, then stop</dd>
        <dt>etcd snapshot</dt>
        <dd>{options.etcdSnapshot === false ? "Off" : "First"}</dd>
        <dt>Between waves</dt>
        <dd>{options.pauseBetweenWaves ? "Waits for a go-ahead" : "Continues on its own"}</dd>
        <dt>When</dt>
        <dd>{sched.mode === "window" ? `${formatWhen(sched.start)} – ${sched.end ? formatWhen(sched.end) : "open"}` : "As soon as approved"}</dd>
        <dt>Readiness</dt>
        <dd>{readinessWords(job.readiness?.summary)} · {timeAgo(job.readiness?.checkedAt)}</dd>
      </dl>

      {job.note ? <blockquote className="uc-quote">{job.note}</blockquote> : null}

      {job.waivers?.length ? (
        <div className="uc-stack uc-gap-6">
          <span className="uc-eyebrow">Waived blockers</span>
          {job.waivers.map((w) => (
            <div key={w.key} className="uc-waived">
              <Chip tone="warn">Waived</Chip>
              <span><strong>{w.name}</strong> — {w.reason} <span className="uc-sub">({w.by})</span></span>
            </div>
          ))}
        </div>
      ) : null}
      {job.readiness?.warnings?.length ? (
        <p className="uc-sub">Warnings at planning time: {job.readiness.warnings.join(", ")}.</p>
      ) : null}

      <div className="uc-stack uc-gap-6">
        <span className="uc-eyebrow">Approvals · {approved} of {approvals.required}</span>
        {(approvals.votes || []).length === 0 ? <span className="uc-sub">No votes yet.</span> : null}
        <ul className="uc-votes">
          {(approvals.votes || []).map((v, i) => (
            <li key={i}>
              <Chip tone={v.decision === "approve" ? "ok" : "danger"}>{v.decision === "approve" ? "Approved" : "Rejected"}</Chip>
              <span>{v.name}</span>
              <span className="uc-sub">{timeAgo(v.at)}</span>
              {v.note ? <span className="uc-sub">“{v.note}”</span> : null}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
