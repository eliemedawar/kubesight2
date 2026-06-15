import { useEffect, useMemo, useState } from "react";
import { listAuditLogs } from "../api";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { formatAccessError, isAccessDeniedError } from "../utils/authz.js";

export default function AuditLogsPage() {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [actionFilter, setActionFilter] = useState("");

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        const data = await listAuditLogs({ limit: 200 });
        setEntries(data.items || []);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  const uniqueActors = useMemo(() => {
    const seen = new Set();
    entries.forEach((e) => {
      const actor = e.actorUsername || e.actorUserId;
      if (actor) seen.add(actor);
    });
    return [...seen].sort();
  }, [entries]);

  const filtered = useMemo(() => {
    return entries.filter((e) => {
      const actor = (e.actorUsername || e.actorUserId || "").toLowerCase();
      const action = (e.action || "").toLowerCase();
      if (actorFilter && actor !== actorFilter.toLowerCase()) return false;
      if (actionFilter && !action.includes(actionFilter.toLowerCase())) return false;
      return true;
    });
  }, [entries, actorFilter, actionFilter]);

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <h2>Audit Logs</h2>
        <p className="muted">Security-relevant actions across the control plane.</p>

        {!loading && !isAccessDeniedError(error) && (
          <div className="user-filters" style={{ marginBottom: "1rem" }}>
            <label className="user-filters__search">
              Actor
              <select
                value={actorFilter}
                onChange={(e) => setActorFilter(e.target.value)}
              >
                <option value="">All actors</option>
                {uniqueActors.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>
            </label>
            <label className="user-filters__search">
              Action
              <input
                type="search"
                placeholder="Filter by action…"
                value={actionFilter}
                onChange={(e) => setActionFilter(e.target.value)}
              />
            </label>
            {(actorFilter || actionFilter) && (
              <button
                type="button"
                className="btn-outline btn-compact"
                style={{ alignSelf: "flex-end" }}
                onClick={() => { setActorFilter(""); setActionFilter(""); }}
              >
                Clear
              </button>
            )}
          </div>
        )}

        {loading ? (
          <p className="muted">Loading audit logs...</p>
        ) : isAccessDeniedError(error) ? (
          <AccessDeniedPage message={error} />
        ) : formatAccessError(error) ? (
          <ErrorBanner message={error} suppressAccessDenied={false} />
        ) : null}

        {!loading && !isAccessDeniedError(error) ? (
          <>
            <p className="muted" style={{ marginBottom: "0.5rem" }}>
              Showing {filtered.length} of {entries.length} entries
            </p>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Date/Time</th>
                    <th>Actor</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="muted">No entries match the current filters.</td>
                    </tr>
                  ) : (
                    filtered.map((entry) => (
                      <tr key={entry.id}>
                        <td>{entry.createdAt ? new Date(entry.createdAt).toLocaleString() : "—"}</td>
                        <td>{entry.actorUsername || entry.actorUserId || "—"}</td>
                        <td>{entry.action}</td>
                        <td>
                          {entry.targetType}
                          {entry.targetId ? `: ${entry.targetId}` : ""}
                        </td>
                        <td>
                          <code className="audit-details">
                            {JSON.stringify(entry.details || {})}
                          </code>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </section>
    </div>
  );
}
