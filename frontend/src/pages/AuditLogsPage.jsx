import { useEffect, useMemo, useState } from "react";
import { listAuditLogs, listClusters } from "../api";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { formatAccessError, isAccessDeniedError } from "../utils/authz.js";
import SearchableSelect from "../components/common/SearchableSelect.jsx";

// Audit entries reference a cluster in a few ways depending on the action:
// deployment requests store it in details.clusterId; cluster/namespace/resource
// targets encode it in targetId (e.g. "cluster" or "cluster/namespace/name").
const CLUSTER_PREFIXED_TARGETS = ["namespace", "pod", "deployment", "service", "resource"];

function clusterOf(entry) {
  const details = entry.details || {};
  if (details.clusterId) return String(details.clusterId);
  if (details.cluster) return String(details.cluster);
  const targetId = entry.targetId || "";
  if (entry.targetType === "cluster" && targetId) return targetId;
  if (CLUSTER_PREFIXED_TARGETS.includes(entry.targetType) && targetId.includes("/")) {
    return targetId.split("/")[0];
  }
  return null;
}

export default function AuditLogsPage() {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [actionFilter, setActionFilter] = useState("");
  const [clusterFilter, setClusterFilter] = useState("");
  const [clusterNameById, setClusterNameById] = useState({});

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
      // Resolve cluster IDs to names (best-effort; auditors may lack cluster view).
      try {
        const clustersRes = await listClusters();
        const map = {};
        (clustersRes.items || []).forEach((c) => {
          if (c.id) map[c.id] = c.name || c.id;
        });
        setClusterNameById(map);
      } catch {
        // Ignore — fall back to IDs (and any names found in audit details).
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

  // Merge names from the cluster list with any clusterName captured in audit
  // details (covers clusters that were since deleted).
  const clusterLabels = useMemo(() => {
    const map = {};
    entries.forEach((e) => {
      const id = clusterOf(e);
      const name = e.details?.clusterName || e.details?.cluster_name;
      if (id && name) map[id] = String(name);
    });
    return { ...map, ...clusterNameById };
  }, [entries, clusterNameById]);

  const clusterLabel = (id) => clusterLabels[id] || id;

  const uniqueClusters = useMemo(() => {
    const seen = new Set();
    entries.forEach((e) => {
      const cluster = clusterOf(e);
      if (cluster) seen.add(cluster);
    });
    return [...seen].sort((a, b) =>
      clusterLabel(a).localeCompare(clusterLabel(b))
    );
  }, [entries, clusterLabels]);

  const filtered = useMemo(() => {
    return entries.filter((e) => {
      const actor = (e.actorUsername || e.actorUserId || "").toLowerCase();
      const action = (e.action || "").toLowerCase();
      if (actorFilter && actor !== actorFilter.toLowerCase()) return false;
      if (actionFilter && !action.includes(actionFilter.toLowerCase())) return false;
      if (clusterFilter && clusterOf(e) !== clusterFilter) return false;
      return true;
    });
  }, [entries, actorFilter, actionFilter, clusterFilter]);

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <h2>Audit Logs</h2>
        <p className="muted">Security-relevant actions across the control plane.</p>

        {!loading && !isAccessDeniedError(error) && (
          <div className="user-filters" style={{ marginBottom: "1rem" }}>
            <label className="user-filters__search">
              Actor
              <SearchableSelect
                value={actorFilter}
                onChange={(e) => setActorFilter(e.target.value)}
              >
                <option value="">All actors</option>
                {uniqueActors.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </SearchableSelect>
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
            <label className="user-filters__search">
              Cluster
              <SearchableSelect
                value={clusterFilter}
                onChange={(e) => setClusterFilter(e.target.value)}
              >
                <option value="">All clusters</option>
                {uniqueClusters.map((c) => (
                  <option key={c} value={c}>{clusterLabel(c)}</option>
                ))}
              </SearchableSelect>
            </label>
            {(actorFilter || actionFilter || clusterFilter) && (
              <button
                type="button"
                className="btn-outline btn-compact"
                style={{ alignSelf: "flex-end" }}
                onClick={() => { setActorFilter(""); setActionFilter(""); setClusterFilter(""); }}
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
