import { useEffect, useState } from "react";
import { listAuditLogs } from "../api";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { formatAccessError, isAccessDeniedError } from "../utils/authz.js";

export default function AuditLogsPage() {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

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

  return (
    <div className="ops-page">
    <section className="card ops-section">
      <h2>Audit Logs</h2>
      <p className="muted">Security-relevant actions across the control plane.</p>
      {loading ? (
        <p className="muted">Loading audit logs...</p>
      ) : isAccessDeniedError(error) ? (
        <AccessDeniedPage message={error} />
      ) : formatAccessError(error) ? (
        <ErrorBanner message={error} suppressAccessDenied={false} />
      ) : null}
      {!loading && !isAccessDeniedError(error) ? (
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
              {entries.map((entry) => (
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
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
    </div>
  );
}
