import { useState } from "react";
import { formatRelative } from "./ciShared.jsx";
import { approvalOutcome, approvalsSoFar, describeApprovers, timeLeft } from "./pipeline/approvalModel.js";

/**
 * An Approval stage in a build: what approvers are asked, who may answer, the
 * answers so far, and — while it waits — Approve / Reject with a comment.
 *
 * The buttons are disabled with the server's own reason when the viewer may
 * not answer (not an approver, started the build, already approved), so a
 * refusal is read before the click rather than after it.
 */
export default function ApprovalStagePanel({ stage, busy, onDecide }) {
  const [comment, setComment] = useState("");
  const state = stage?.approval;
  if (!state) return null;
  const outcome = approvalOutcome(state);
  const viewer = state.viewer || {};
  const waiting = stage.status === "running" && state.phase === "waiting_approval";
  const approvals = approvalsSoFar(state);
  const decisions = state.decisions || [];
  const decide = (action) => {
    onDecide?.(action, comment.trim());
    setComment("");
  };

  return (
    <section className={`sg-ci-deploy sg-ci-approval is-${outcome?.tone || "info"}`} aria-label="Approval">
      <header>
        <span className={`sg-ci-deploy-badge is-${outcome?.tone || "info"}`}>{outcome?.label}</span>
        {waiting && state.deadlineAt && (
          <span className="sg-ci-approval-deadline">fails if not approved {timeLeft(state.deadlineAt)}</span>
        )}
      </header>
      {state.instructions && <p className="sg-ci-approval-message">{state.instructions}</p>}
      <dl>
        <dt>Who may approve</dt>
        <dd>
          {describeApprovers(state.approvers)}
          {state.allowSelfApproval ? "" : state.startedBy?.username ? ` — not ${state.startedBy.username}, who started it` : ""}
        </dd>
        <dt>Approvals</dt>
        <dd>
          {approvals.length} of {state.required || 1}
        </dd>
        {state.notified && (
          <>
            <dt>Emailed</dt>
            <dd>{state.notified.recipients?.length ? state.notified.recipients.join(", ") : "nobody had an email address"}</dd>
          </>
        )}
      </dl>
      {decisions.length > 0 && (
        <ol className="sg-ci-approval-decisions" aria-label="Decisions">
          {decisions.map((item, position) => (
            <li key={position} className={`is-${item.decision}`}>
              <span className="sg-ci-approval-verdict">{item.decision === "approve" ? "Approved" : "Rejected"}</span>
              <strong>{item.username}</strong>
              <span className="muted">{formatRelative(item.at)}</span>
              {item.comment && <q>{item.comment}</q>}
            </li>
          ))}
        </ol>
      )}
      {waiting && (
        <div className="sg-ci-approval-act">
          <label className="sg-ci-approval-comment">
            <span>Comment (optional)</span>
            <textarea
              rows={2}
              maxLength={1000}
              value={comment}
              disabled={busy || !(viewer.canApprove || viewer.canReject)}
              placeholder="Why — kept with the decision and in the audit log."
              onChange={(event) => setComment(event.target.value)}
            />
          </label>
          <div className="sg-ci-approval-buttons">
            <button
              type="button"
              className="btn-primary btn-compact"
              disabled={busy || !viewer.canApprove}
              title={viewer.canApprove ? undefined : viewer.reason}
              onClick={() => decide("approve")}
            >
              Approve
            </button>
            <button
              type="button"
              className="btn-outline btn-compact danger"
              disabled={busy || !viewer.canReject}
              title={viewer.canReject ? undefined : viewer.rejectReason}
              onClick={() => decide("reject")}
            >
              Reject
            </button>
          </div>
          {!viewer.canApprove && viewer.reason && <p className="field-hint">{viewer.reason}</p>}
        </div>
      )}
    </section>
  );
}
