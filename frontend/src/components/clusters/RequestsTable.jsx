import { relativeTime } from "../../lib/relativeTime.js";
import { Fragment } from "react";

export const STATUS_TONE = {
  pending: "warn",
  approved: "ok",
  declined: "danger",
};

export function formatDate(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

/* Compact relative age for table rows ("26m", "3h", "2d"). */
export function timeAgo(value) {
  // Compact, because this is a narrow column. Delegates so it parses naive
  // backend timestamps as UTC — this copy read them as local, which shifted
  // every age by the viewer's offset.
  return relativeTime(value, { style: "compact", empty: "—" });
}

/* Long-form waiting time for decision cards ("26 min", "1 h 12 min"). */
export function waitingFor(value) {
  if (!value) return "—";
  const t = new Date(value).getTime();
  if (!Number.isFinite(t)) return "—";
  const mins = Math.floor(Math.max(0, Date.now() - t) / 60000);
  if (mins < 1) return "<1 min";
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  if (h < 24) {
    const rem = mins % 60;
    return rem ? `${h} h ${rem} min` : `${h} h`;
  }
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH ? `${d} d ${remH} h` : `${d} d`;
}

/* Initials for avatars from a full name or an email address. */
export function initialsOf(value) {
  const raw = String(value || "").trim();
  if (!raw) return "?";
  const base = raw.includes("@") ? raw.split("@")[0] : raw;
  const parts = base.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return base.slice(0, 2).toUpperCase();
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/* Environment tone inferred from the cluster name (presentation only). */
export function clusterEnvTone(name) {
  const n = String(name || "").toLowerCase();
  if (
    n.includes("stag") ||
    n.includes("stg") ||
    n.includes("uat") ||
    n.includes("preprod") ||
    n.includes("pre-prod")
  )
    return "staging";
  if (n.includes("prod") || n.includes("prd")) return "prod";
  if (n.includes("dev") || n.includes("test") || n.includes("qa") || n.includes("sandbox"))
    return "dev";
  return "";
}

export function ClusterTag({ name }) {
  const tone = clusterEnvTone(name);
  return <span className={`sg-tag${tone ? ` sg-tag--${tone}` : ""}`}>{name || "—"}</span>;
}

/* ── Inline stroke icons (viewBox 24, strokeWidth 1.8) ─────────────── */

export function IconCheck(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export function IconX(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

export function IconClock(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15.5 14" />
    </svg>
  );
}

/* ── Pipeline chips ─────────────────────────────────────────────────
   Stages are derived ONLY from state the request actually models:
     Submitted — the request exists (createdAt).
     Approval  — quorum progress (approvals / requiredApprovals, votes)
                 → run while pending, done when approved, fail when declined.
     Window    — the requested maintenance window, only when one was set
                 → wait until approved/open, run while open, done once closed.
   The requests API carries no image-check, build, or rollout state, so
   those concept stages are intentionally not rendered here.               */

function approvalTitle(row) {
  const required = row.requiredApprovals ?? 1;
  const approvals = row.approvals ?? 0;
  const declines = row.declines ?? 0;
  const lines = [
    `${approvals} of ${required} approvals${declines ? ` · ${declines} declined` : ""}`,
    ...(row.votes || []).map((v) => `${v.email}: ${v.decision}`),
  ];
  return lines.join("\n");
}

export function derivePipelineStages(row) {
  const required = row.requiredApprovals ?? 1;
  const approvals = row.approvals ?? 0;
  const stages = [
    {
      key: "submitted",
      state: "done",
      label: "Submitted",
      title: `Created ${formatDate(row.createdAt)}`,
    },
  ];

  if (row.status === "approved") {
    stages.push({ key: "approval", state: "done", label: "Approval", title: approvalTitle(row) });
  } else if (row.status === "declined") {
    stages.push({ key: "approval", state: "fail", label: "Declined", title: approvalTitle(row) });
  } else {
    stages.push({
      key: "approval",
      state: "run",
      label: `Approval ${approvals}/${required}`,
      title: approvalTitle(row),
    });
  }

  if (row.requestedWindowStart && row.requestedWindowEnd) {
    const start = new Date(row.requestedWindowStart).getTime();
    const end = new Date(row.requestedWindowEnd).getTime();
    const now = Date.now();
    const title = row.requestedWindowLabel || undefined;
    if (row.status !== "approved" || now < start) {
      stages.push({ key: "window", state: "wait", label: "Window", title });
    } else if (now <= end) {
      stages.push({ key: "window", state: "run", label: "Window open", title });
    } else {
      stages.push({ key: "window", state: "done", label: "Window closed", title });
    }
  }

  return stages;
}

export function PipelineChips({ row }) {
  const stages = derivePipelineStages(row);
  return (
    <div className="sg-pipe">
      {stages.map((stage, i) => (
        <Fragment key={stage.key}>
          {i > 0 ? <i className="sg-pline" /> : null}
          <span className={`sg-pstep sg-pstep--${stage.state}`} title={stage.title}>
            {stage.state === "done" ? <IconCheck /> : null}
            {stage.state === "fail" ? <IconX /> : null}
            {stage.label}
          </span>
        </Fragment>
      ))}
    </div>
  );
}

/* ── Requests table (approver history + requester view) ────────────── */

export default function RequestsTable({ rows, canManage, decide, busyId, emptyLabel }) {
  return (
    <div className="table-wrap">
      <table className="data-table sg-req-table">
        <thead>
          <tr>
            <th>Request</th>
            <th>Cluster</th>
            <th>Pipeline</th>
            <th>Status</th>
            <th>Age</th>
            {canManage ? <th>Actions</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={canManage ? 6 : 5} className="muted">
                {emptyLabel}
              </td>
            </tr>
          ) : (
            rows.map((row) => (
              <tr key={row.id}>
                <td className="sg-req-cell">
                  <span className="sg-req-title" title={row.message}>
                    {row.message}
                  </span>
                  <span className="sg-req-sub">
                    Requested by {row.requesterName}
                    {row.requestedWindowLabel ? (
                      <>
                        {" · "}
                        <span className="sg-req-window">{row.requestedWindowLabel}</span>
                      </>
                    ) : null}
                  </span>
                </td>
                <td>
                  <ClusterTag name={row.clusterName} />
                </td>
                <td>
                  <PipelineChips row={row} />
                </td>
                <td>
                  <span className={`status-pill ${STATUS_TONE[row.status] || "info"}`}>
                    {row.status}
                  </span>
                </td>
                <td className="sg-req-age" title={formatDate(row.createdAt)}>
                  {timeAgo(row.createdAt)}
                </td>
                {canManage ? (
                  <td className="col-actions">
                    {row.status === "pending" ? (
                      <div className="sg-req-actions">
                        <button
                          type="button"
                          className="primary btn-compact"
                          onClick={() => decide(row, "approve")}
                          disabled={busyId === row.id}
                        >
                          <IconCheck className="sg-btn-ic" />
                          Approve
                        </button>
                        <button
                          type="button"
                          className="btn-danger-outline btn-compact"
                          onClick={() => decide(row, "decline")}
                          disabled={busyId === row.id}
                        >
                          Decline
                        </button>
                      </div>
                    ) : (
                      <span className="muted sg-req-decided">
                        {row.status === "approved" ? "Approved" : "Declined"}
                        {row.decidedByName ? ` by ${row.decidedByName}` : ""}
                        {row.decidedAt ? ` · ${formatDate(row.decidedAt)}` : ""}
                      </span>
                    )}
                  </td>
                ) : null}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
