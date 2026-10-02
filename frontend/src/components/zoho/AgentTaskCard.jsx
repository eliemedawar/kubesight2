// What the Hermes ticket agent did with a ticket: one card per task (the
// first reading, then each follow-up), with the approval buttons when Hermes
// asked a human to confirm.

const TASK_PILL = {
  pending: ["info", "Queued for Hermes"],
  running: ["info", "Hermes is on it"],
  executed: ["ok", "Executed"],
  awaiting_approval: ["warn", "Needs approval"],
  deciding: ["info", "Deciding…"],
  impediment: ["danger", "Impediment"],
  on_hold: ["warn", "On hold"],
  done: ["ok", "Done"],
  error: ["danger", "Agent error"],
  superseded: ["muted", "Superseded"],
};

const EVENT_LABEL = {
  run_finished: "Run finished",
  runs_finished: "All runs finished",
  approval_rejected: "Approval rejected",
  approval_expired: "Approval expired",
};

// A troubleshooting answer reads as an answer, not as a change that was done.
const ANSWERED_PILL = {
  on_hold: ["info", "Answered · waiting on requester"],
  done: ["ok", "Answered"],
};

export function AgentTaskPill({ task }) {
  if (!task) return <span className="muted">—</span>;
  const [tone, label] =
    (task.route === "answered" && ANSWERED_PILL[task.status]) ||
    TASK_PILL[task.status] || ["muted", task.status];
  return (
    <span className={`status-pill ${tone}`} title={task.understanding || task.error || ""}>
      {label}
    </span>
  );
}

function describeOne(c) {
  const where = c.deploymentName ? `${c.deploymentName} in ${c.namespace}` : null;
  if (!where) return null;
  if (c.changeType === "env_var") return `set ${c.variable}=${c.value} on ${where}`;
  if (c.changeType === "restart") return `restart ${where}`;
  if (c.changeType === "image") return `deploy ${c.deploymentName} ${c.tag} to ${c.namespace}`;
  return null;
}

// Tasks from before `changes` existed carry the one change in their columns.
const changesOf = (task) =>
  task.changes?.length
    ? task.changes
    : [{ ...task, variable: task.variableName, value: task.variableValue }];

export default function AgentTaskCard({ task, canManage, deciding, onApprove, onReject }) {
  const changes = changesOf(task).filter((c) => describeOne(c));
  const several = changes.length > 1;
  const change = changes.length === 1 ? describeOne(changes[0]) : null;
  const finishedRuns = task.event?.type === "runs_finished" ? task.event.runs || [] : [];
  const replied = task.event?.type === "requester_replied";
  const trouble = task.troubleshooting || null;
  const title =
    task.kind === "followup"
      ? `Follow-up · ${EVENT_LABEL[task.event?.type] || "event"}`
      : replied
      ? "Requester replied — Hermes continued"
      : trouble
      ? "Hermes investigated the problem"
      : "Hermes read the ticket";
  return (
    <div className="sg-zh-run sg-zh-agent">
      <div className="sg-zh-run-head">
        <b>{title}</b>
        {task.confidence ? <span className="sg-tag">confidence {task.confidence}</span> : null}
        {change ? <span className="sg-tag mono">{change}</span> : null}
        {several ? <span className="sg-tag">{changes.length} applications</span> : null}
        <span className="sg-zh-run-spacer" />
        <span className="sg-zh-htime">
          {task.createdAt ? new Date(task.createdAt).toLocaleString() : ""}
        </span>
        <AgentTaskPill task={task} />
      </div>

      {replied
        ? (task.event.comments || []).map((c, index) => (
            <p key={c.id || index} className="sg-zh-agent-line">
              <span className="muted">{c.author || "Requester"}:</span> {c.text}
            </p>
          ))
        : null}
      {trouble ? (
        <div className="sg-zh-diag" aria-label="Hermes' diagnosis">
          <p className="sg-zh-agent-line">
            <span className="muted">Diagnosis:</span> {trouble.diagnosis}
          </p>
          {trouble.findings?.length ? (
            <ul className="sg-zh-diag-findings" aria-label="Evidence">
              {trouble.findings.map((f, index) => (
                <li key={`${f.finding}-${index}`}>
                  <span>{f.finding}</span>
                  {f.evidence ? <span className="sg-zh-diag-evidence mono">{f.evidence}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
          {trouble.checked?.length ? (
            <p className="sg-zh-fhint">
              <span className="muted">Checked and healthy:</span> {trouble.checked.join(" · ")}
            </p>
          ) : null}
          <p className="sg-zh-agent-line sg-zh-diag-rec">
            <span className="muted">Recommendation:</span> {trouble.recommendation}
          </p>
          {task.status === "awaiting_approval" && change ? (
            <p className="sg-zh-fhint">Hermes proposes this fix; it runs only if approved.</p>
          ) : null}
        </div>
      ) : task.understanding ? (
        <p className="sg-zh-agent-line">
          <span className="muted">Understood:</span> {task.understanding}
        </p>
      ) : null}
      {several ? (
        <ul className="sg-zh-agent-changes" aria-label="What Hermes is changing">
          {changes.map((c, index) => (
            <li key={`${c.namespace}/${c.deploymentName}/${index}`}>
              <span className="sg-tag mono">{describeOne(c)}</span>
              {c.runId ? <span className="sg-zh-run-ref">run #{c.runId}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {finishedRuns.length ? (
        <ul className="sg-zh-agent-changes" aria-label="How each run ended">
          {finishedRuns.map((r) => (
            <li key={r.runId}>
              <span className={`status-pill ${r.result === "deployed" ? "ok" : r.result === "failed" ? "danger" : "muted"}`}>
                {r.result}
              </span>
              <span className="mono">
                {r.deployment} ({r.namespace})
              </span>
              <span className="sg-zh-run-ref">run #{r.runId}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {task.comment ? (
        <blockquote className="sg-zh-agent-quote" title="Hermes' comment on the ticket">
          {task.comment}
        </blockquote>
      ) : null}
      {task.reasons?.length ? (
        <ul className="sg-zh-agent-reasons">
          {task.reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}
      {task.status === "awaiting_approval" ? (
        <p className="sg-zh-fhint">
          {task.telegramSent ? "Sent to Telegram. " : ""}
          {task.expiresAt ? `Expires ${new Date(task.expiresAt).toLocaleString()}.` : ""}
        </p>
      ) : null}
      {task.decidedBy ? (
        <p className="sg-zh-fhint">
          Decided by {task.decidedBy}
          {task.decisionNote ? ` — ${task.decisionNote}` : ""}
        </p>
      ) : null}
      {task.finalMessage ? <p className="sg-zh-fhint">Hermes: {task.finalMessage}</p> : null}
      {task.error ? <p className="sg-zh-inline-error">{task.error}</p> : null}

      {task.status === "awaiting_approval" && canManage ? (
        <div className="sg-zh-agent-actions">
          <button type="button" className="primary" disabled={deciding} onClick={() => onApprove(task)}>
            Approve and run
          </button>
          <button type="button" className="secondary" disabled={deciding} onClick={() => onReject(task)}>
            Reject
          </button>
        </div>
      ) : null}
    </div>
  );
}
