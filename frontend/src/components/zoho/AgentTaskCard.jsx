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
  approval_rejected: "Approval rejected",
  approval_expired: "Approval expired",
};

export function AgentTaskPill({ task }) {
  if (!task) return <span className="muted">—</span>;
  const [tone, label] = TASK_PILL[task.status] || ["muted", task.status];
  return (
    <span className={`status-pill ${tone}`} title={task.understanding || task.error || ""}>
      {label}
    </span>
  );
}

function describe(task) {
  const where = task.deploymentName ? `${task.deploymentName} in ${task.namespace}` : null;
  if (!where) return null;
  if (task.changeType === "env_var") return `set ${task.variableName}=${task.variableValue} on ${where}`;
  if (task.changeType === "restart") return `restart ${where}`;
  if (task.changeType === "image") return `deploy ${task.deploymentName} ${task.tag} to ${task.namespace}`;
  return null;
}

export default function AgentTaskCard({ task, canManage, deciding, onApprove, onReject }) {
  const change = describe(task);
  const replied = task.event?.type === "requester_replied";
  const title =
    task.kind === "followup"
      ? `Follow-up · ${EVENT_LABEL[task.event?.type] || "event"}`
      : replied
      ? "Requester replied — Hermes continued"
      : "Hermes read the ticket";
  return (
    <div className="sg-zh-run sg-zh-agent">
      <div className="sg-zh-run-head">
        <b>{title}</b>
        {task.confidence ? <span className="sg-tag">confidence {task.confidence}</span> : null}
        {change ? <span className="sg-tag mono">{change}</span> : null}
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
      {task.understanding ? (
        <p className="sg-zh-agent-line">
          <span className="muted">Understood:</span> {task.understanding}
        </p>
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
