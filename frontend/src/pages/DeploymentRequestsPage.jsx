import { useCallback, useEffect, useMemo, useState } from "react";
import {
  listDeploymentRequests,
  approveDeploymentRequest,
  declineDeploymentRequest,
} from "../api";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { usePermission } from "../hooks/usePermission.js";
import { formatAccessError, isAccessDeniedError } from "../utils/authz.js";

const STATUS_TONE = {
  pending: "warn",
  approved: "ok",
  declined: "danger",
};

const TABS = [
  { key: "active", label: "Active Requests" },
  { key: "history", label: "Request History" },
];

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

function RequestsTable({ rows, canManage, decide, busyId, emptyLabel }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Requester</th>
            <th>Cluster</th>
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
              <td colSpan={canManage ? 7 : 6} className="muted">
                {emptyLabel}
              </td>
            </tr>
          ) : (
            rows.map((row) => (
              <tr key={row.id}>
                <td>{row.requesterName}</td>
                <td>{row.clusterName}</td>
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

export default function DeploymentRequestsPage() {
  const { hasPermission } = usePermission();
  const canManage = hasPermission("deployment_requests:manage");

  const [activeTab, setActiveTab] = useState("active");
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busyId, setBusyId] = useState(null);

  // History filters
  const [statusFilter, setStatusFilter] = useState("all");
  const [clusterFilter, setClusterFilter] = useState("all");
  const [requesterFilter, setRequesterFilter] = useState("all");
  const [search, setSearch] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listDeploymentRequests({ limit: 200 });
      setItems(data.items || []);
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const decide = async (request, action) => {
    setBusyId(request.id);
    setActionError("");
    try {
      const fn = action === "approve" ? approveDeploymentRequest : declineDeploymentRequest;
      const updated = await fn(request.id);
      setItems((prev) => prev.map((row) => (row.id === updated.id ? updated : row)));
    } catch (err) {
      setActionError(err.message || `Failed to ${action} request.`);
    } finally {
      setBusyId(null);
    }
  };

  const activeRequests = useMemo(
    () => items.filter((row) => row.status === "pending"),
    [items]
  );
  const historyRequests = useMemo(
    () => items.filter((row) => row.status !== "pending"),
    [items]
  );

  const clusterOptions = useMemo(
    () =>
      Array.from(new Set(historyRequests.map((r) => r.clusterName).filter(Boolean))).sort(),
    [historyRequests]
  );
  const requesterOptions = useMemo(
    () =>
      Array.from(new Set(historyRequests.map((r) => r.requesterName).filter(Boolean))).sort(),
    [historyRequests]
  );

  const filteredHistory = useMemo(() => {
    const q = search.trim().toLowerCase();
    return historyRequests.filter((row) => {
      if (statusFilter !== "all" && row.status !== statusFilter) return false;
      if (clusterFilter !== "all" && row.clusterName !== clusterFilter) return false;
      if (requesterFilter !== "all" && row.requesterName !== requesterFilter) return false;
      if (q && !(`${row.message} ${row.clusterName} ${row.requesterName}`.toLowerCase().includes(q)))
        return false;
      return true;
    });
  }, [historyRequests, statusFilter, clusterFilter, requesterFilter, search]);

  const resetFilters = () => {
    setStatusFilter("all");
    setClusterFilter("all");
    setRequesterFilter("all");
    setSearch("");
  };
  const filtersActive =
    statusFilter !== "all" || clusterFilter !== "all" || requesterFilter !== "all" || search.trim();

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <div className="card-header-row" style={{ marginBottom: "var(--space-2)" }}>
          <div>
            <h2>Deployment Requests</h2>
            <p className="muted">
              Requests to deploy or change clusters, routed to the management team for approval.
            </p>
          </div>
          {!isAccessDeniedError(error) ? (
            <button type="button" className="btn-outline btn-compact" onClick={load} disabled={loading}>
              Refresh
            </button>
          ) : null}
        </div>

        <nav className="tab-bar" aria-label="Deployment request views">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              className={activeTab === tab.key ? "active" : ""}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.label}
              {tab.key === "active" ? ` (${activeRequests.length})` : ""}
            </button>
          ))}
        </nav>

        {actionError ? <ErrorBanner message={actionError} suppressAccessDenied={false} /> : null}

        {loading ? (
          <p className="muted">Loading deployment requests...</p>
        ) : isAccessDeniedError(error) ? (
          <AccessDeniedPage message={error} />
        ) : formatAccessError(error) ? (
          <ErrorBanner message={error} suppressAccessDenied={false} />
        ) : activeTab === "active" ? (
          <RequestsTable
            rows={activeRequests}
            canManage={canManage}
            decide={decide}
            busyId={busyId}
            emptyLabel="No active requests awaiting a decision."
          />
        ) : (
          <>
            <div className="user-filters" style={{ marginBottom: "var(--space-3)" }}>
              <label className="user-filters__search">
                Search
                <input
                  type="search"
                  placeholder="Message, cluster, or requester"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
              <label>
                Status
                <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                  <option value="all">All</option>
                  <option value="approved">Approved</option>
                  <option value="declined">Declined</option>
                </select>
              </label>
              <label>
                Cluster
                <select value={clusterFilter} onChange={(e) => setClusterFilter(e.target.value)}>
                  <option value="all">All clusters</option>
                  {clusterOptions.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Requester
                <select value={requesterFilter} onChange={(e) => setRequesterFilter(e.target.value)}>
                  <option value="all">All requesters</option>
                  {requesterOptions.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              {filtersActive ? (
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  style={{ alignSelf: "flex-end" }}
                  onClick={resetFilters}
                >
                  Clear
                </button>
              ) : null}
            </div>

            <p className="muted" style={{ marginBottom: "0.5rem" }}>
              Showing {filteredHistory.length} of {historyRequests.length} past requests
            </p>

            <RequestsTable
              rows={filteredHistory}
              canManage={canManage}
              decide={decide}
              busyId={busyId}
              emptyLabel={
                historyRequests.length
                  ? "No requests match the current filters."
                  : "No decided requests yet."
              }
            />
          </>
        )}
      </section>
    </div>
  );
}
