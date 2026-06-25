const STATUS_TONE = {
  pending: "warn",
  approved: "ok",
  declined: "danger",
};

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

export default function RequestsTable({ rows, canManage, decide, busyId, emptyLabel }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Requester</th>
            <th>Cluster</th>
            <th>Requested window</th>
            <th>Message</th>
            <th>Status</th>
            <th>Approvals</th>
            <th>Created</th>
            {canManage ? <th>Actions</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={canManage ? 8 : 7} className="muted">
                {emptyLabel}
              </td>
            </tr>
          ) : (
            rows.map((row) => (
              <tr key={row.id}>
                <td>{row.requesterName}</td>
                <td>{row.clusterName}</td>
                <td className="muted" style={{ whiteSpace: "nowrap" }}>
                  {row.requestedWindowLabel || "—"}
                </td>
                <td style={{ maxWidth: "28rem", whiteSpace: "pre-wrap" }}>{row.message}</td>
                <td>
                  <span className={`status-pill ${STATUS_TONE[row.status] || "info"}`}>
                    {row.status}
                  </span>
                </td>
                <td title={(row.votes || []).map((v) => `${v.email}: ${v.decision}`).join("\n")}>
                  {row.approvals ?? 0} / {row.requiredApprovals ?? 1}
                  {row.declines ? ` (${row.declines} declined)` : ""}
                </td>
                <td>{formatDate(row.createdAt)}</td>
                {canManage ? (
                  <td className="col-actions">
                    {row.status === "pending" ? (
                      <div style={{ display: "flex", gap: "var(--space-2)" }}>
                        <button
                          type="button"
                          className="btn-primary btn-compact"
                          onClick={() => decide(row, "approve")}
                          disabled={busyId === row.id}
                        >
                          Approve
                        </button>
                        <button
                          type="button"
                          className="btn-outline btn-compact"
                          onClick={() => decide(row, "decline")}
                          disabled={busyId === row.id}
                        >
                          Decline
                        </button>
                      </div>
                    ) : (
                      <span className="muted">
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
