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
import SearchableSelect from "../components/common/SearchableSelect.jsx";
import RequestsTable, {
  ClusterTag,
  IconCheck,
  IconClock,
  formatDate,
  initialsOf,
  waitingFor,
} from "../components/clusters/RequestsTable.jsx";

const TABS = [
  { key: "active", label: "Active Requests" },
  { key: "history", label: "Request History" },
];

/* Decision card for one pending request (Signal "Approvals" anatomy).
   Real fields only: cluster, requester, free-text message, optional
   requested window, quorum votes. Approve/decline reuse the existing
   handlers passed down from the page. */
function RequestDecisionCard({ row, canManage, decide, busy }) {
  const required = row.requiredApprovals ?? 1;
  const approvals = row.approvals ?? 0;
  const declines = row.declines ?? 0;
  const votes = row.votes || [];
  const pendingSlots = Math.max(0, required - approvals);

  const note =
    votes.length === 0
      ? `Waiting on ${required} approver${required === 1 ? "" : "s"}`
      : `${approvals} of ${required} approved${declines ? ` · ${declines} declined` : ""}`;

  return (
    <article className="sg-rq">
      <header>
        <div className="sg-rq-head-tags">
          <b>Deploy request #{row.id}</b>
          <ClusterTag name={row.clusterName} />
          <span className={`status-pill ${approvals > 0 ? "warn" : "info"}`}>
            {approvals} of {required} approvals
          </span>
        </div>
        <span className="sg-rq-wait" title={`Created ${formatDate(row.createdAt)}`}>
          <IconClock />
          {waitingFor(row.createdAt)}
        </span>
      </header>

      <div className="sg-rq-meta">
        <span className="sg-avatar sg-avatar--sm">{initialsOf(row.requesterName)}</span>
        <span>
          Requested by <b>{row.requesterName}</b>
          {row.requesterUsername ? ` · ${row.requesterUsername}` : ""}
        </span>
      </div>

      {row.message ? <p className="sg-rq-msg">{row.message}</p> : null}

      {row.requestedWindowLabel ? (
        <div className="sg-rq-diff">
          <span className="sg-dchip">
            window <b>{row.requestedWindowLabel}</b>
          </span>
        </div>
      ) : null}

      <footer>
        <div className="sg-rq-approvers">
          {votes.map((vote) => (
            <span
              key={vote.email}
              className={`sg-avatar sg-avatar--sm ${
                vote.decision === "approve" ? "sg-vote--approve" : "sg-vote--decline"
              }`}
              title={`${vote.email} · ${vote.decision === "approve" ? "approved" : "declined"}${
                vote.at ? ` · ${formatDate(vote.at)}` : ""
              }`}
            >
              {initialsOf(vote.email)}
            </span>
          ))}
          {Array.from({ length: pendingSlots }).map((_, i) => (
            <span key={`slot-${i}`} className="sg-avatar sg-avatar--sm sg-vote--empty" aria-hidden="true" />
          ))}
          <span className="sg-rq-approvers-note">{note}</span>
        </div>
        {canManage ? (
          <div className="sg-rq-actions">
            <button
              type="button"
              className="btn-danger-outline btn-compact"
              onClick={() => decide(row, "decline")}
              disabled={busy}
            >
              Decline
            </button>
            <button
              type="button"
              className="primary btn-compact"
              onClick={() => decide(row, "approve")}
              disabled={busy}
            >
              <IconCheck className="sg-btn-ic" />
              Approve
            </button>
          </div>
        ) : null}
      </footer>
    </article>
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

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const data = await listDeploymentRequests({ limit: 200 });
      setItems(data.items || []);
      setError("");
    } catch (err) {
      if (!silent) setError(err.message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Approvals can be granted elsewhere (management team email links, another
  // manager in-app). Poll quietly so a request that reaches its quorum flips to
  // "approved" here without needing a manual refresh.
  useEffect(() => {
    const id = setInterval(() => load({ silent: true }), 15000);
    return () => clearInterval(id);
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
        <div className="sg-ph">
          <div>
            <h2>Deployment Requests</h2>
            <p className="sg-ph-sub">
              Requests to deploy or change clusters, routed to the management team for approval.
            </p>
          </div>
          <div className="sg-ph-actions">
            {!isAccessDeniedError(error) ? (
              <button type="button" className="btn-outline btn-compact" onClick={load} disabled={loading}>
                Refresh
              </button>
            ) : null}
          </div>
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
              {tab.key === "active" ? (
                <span className={`sg-cnt${activeRequests.length === 0 ? " sg-cnt--zero" : ""}`}>
                  {activeRequests.length}
                </span>
              ) : null}
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
          activeRequests.length === 0 ? (
            <p className="muted">No active requests awaiting a decision.</p>
          ) : (
            <div className="sg-rq-list">
              {activeRequests.map((row) => (
                <RequestDecisionCard
                  key={row.id}
                  row={row}
                  canManage={canManage}
                  decide={decide}
                  busy={busyId === row.id}
                />
              ))}
            </div>
          )
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
                <SearchableSelect value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                  <option value="all">All</option>
                  <option value="approved">Approved</option>
                  <option value="declined">Declined</option>
                </SearchableSelect>
              </label>
              <label>
                Cluster
                <SearchableSelect value={clusterFilter} onChange={(e) => setClusterFilter(e.target.value)}>
                  <option value="all">All clusters</option>
                  {clusterOptions.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </SearchableSelect>
              </label>
              <label>
                Requester
                <SearchableSelect value={requesterFilter} onChange={(e) => setRequesterFilter(e.target.value)}>
                  <option value="all">All requesters</option>
                  {requesterOptions.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </SearchableSelect>
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
