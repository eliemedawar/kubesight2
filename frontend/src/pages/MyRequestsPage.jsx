import { useCallback, useEffect, useMemo, useState } from "react";
import { listMyDeploymentRequests } from "../api";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { formatAccessError, isAccessDeniedError } from "../utils/authz.js";
import SearchableSelect from "../components/common/SearchableSelect.jsx";
import RequestsTable from "../components/clusters/RequestsTable.jsx";

const TABS = [
  { key: "active", label: "Active" },
  { key: "history", label: "History" },
];

export default function MyRequestsPage() {
  const [activeTab, setActiveTab] = useState("active");
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // History filters
  const [statusFilter, setStatusFilter] = useState("all");
  const [clusterFilter, setClusterFilter] = useState("all");
  const [search, setSearch] = useState("");

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true);
    try {
      const data = await listMyDeploymentRequests({ limit: 200 });
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

  // Approvals are granted elsewhere (management team email links, in-app by a
  // manager). Poll quietly so a request that reaches its quorum flips status
  // here without needing a manual refresh.
  useEffect(() => {
    const id = setInterval(() => load({ silent: true }), 15000);
    return () => clearInterval(id);
  }, [load]);

  // A request is "active" while it still needs a decision (pending) or while it
  // is approved and its requested window has not yet ended — an approval stays
  // usable until the window closes, so it should stay visible here until then.
  const isActiveRequest = useCallback((row) => {
    if (row.status === "pending") return true;
    if (row.status === "approved" && row.requestedWindowEnd) {
      const end = new Date(row.requestedWindowEnd).getTime();
      return Number.isFinite(end) && Date.now() <= end;
    }
    return false;
  }, []);

  const activeRequests = useMemo(
    () => items.filter(isActiveRequest),
    [items, isActiveRequest]
  );
  const historyRequests = useMemo(
    () => items.filter((row) => !isActiveRequest(row)),
    [items, isActiveRequest]
  );

  const clusterOptions = useMemo(
    () =>
      Array.from(new Set(historyRequests.map((r) => r.clusterName).filter(Boolean))).sort(),
    [historyRequests]
  );

  const filteredHistory = useMemo(() => {
    const q = search.trim().toLowerCase();
    return historyRequests.filter((row) => {
      if (statusFilter !== "all" && row.status !== statusFilter) return false;
      if (clusterFilter !== "all" && row.clusterName !== clusterFilter) return false;
      if (q && !(`${row.message} ${row.clusterName}`.toLowerCase().includes(q))) return false;
      return true;
    });
  }, [historyRequests, statusFilter, clusterFilter, search]);

  const resetFilters = () => {
    setStatusFilter("all");
    setClusterFilter("all");
    setSearch("");
  };
  const filtersActive =
    statusFilter !== "all" || clusterFilter !== "all" || search.trim();

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <div className="card-header-row" style={{ marginBottom: "var(--space-2)" }}>
          <div>
            <h2>My Requests</h2>
            <p className="muted">
              Track the status of deployment requests you've submitted.
            </p>
          </div>
          {!isAccessDeniedError(error) ? (
            <button type="button" className="btn-outline btn-compact" onClick={load} disabled={loading}>
              Refresh
            </button>
          ) : null}
        </div>

        <nav className="tab-bar" aria-label="My request views">
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

        {loading ? (
          <p className="muted">Loading your requests...</p>
        ) : isAccessDeniedError(error) ? (
          <AccessDeniedPage message={error} />
        ) : formatAccessError(error) ? (
          <ErrorBanner message={error} suppressAccessDenied={false} />
        ) : activeTab === "active" ? (
          <RequestsTable
            rows={activeRequests}
            canManage={false}
            emptyLabel="No active requests awaiting a decision."
          />
        ) : (
          <>
            <div className="user-filters" style={{ marginBottom: "var(--space-3)" }}>
              <label className="user-filters__search">
                Search
                <input
                  type="search"
                  placeholder="Message or cluster"
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
              canManage={false}
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
