import { useEffect, useMemo, useRef, useState } from "react";
import { exportAuditLogs, listAuditActors, listAuditLogs, listClusters } from "../api";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { formatAccessError, isAccessDeniedError } from "../utils/authz.js";
import SearchableSelect from "../components/common/SearchableSelect.jsx";
import { parseApiTime } from "../lib/apiTime.js";
import { QUICK_RANGES, describeRange, localDay, rangeBounds } from "../lib/auditLogRange.js";

const PAGE_SIZE = 200;
// Typing in the action box asks the server once the typing pauses.
const ACTION_DEBOUNCE_MS = 350;

// createdAt is naive UTC; read as local it was off by the viewer's UTC offset.
function formatWhen(value) {
  const ms = parseApiTime(value);
  return Number.isFinite(ms) ? new Date(ms).toLocaleString() : "—";
}

// Audit entries reference a cluster in a few ways depending on the action:
// deployment requests store it in details.clusterId; cluster/namespace/resource
// targets encode it in targetId (e.g. "cluster" or "cluster/namespace/name").
const CLUSTER_PREFIXED_TARGETS = ["namespace", "pod", "deployment", "service", "resource"];

// System-initiated actions (scheduler, webhook, deploy automation) have no
// human actor — shown, filtered and exported under this label.
const AUTOMATION_ACTOR = "KubeSight automation";

function displayActor(entry) {
  return entry.actorUsername || entry.actorUserId || AUTOMATION_ACTOR;
}

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
  // `ready` flips once the first answer is in (it decides between the page
  // and Access denied); `loading` is any reload for new filters after that.
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [actorFilter, setActorFilter] = useState("");
  const [actionInput, setActionInput] = useState("");
  const [actionFilter, setActionFilter] = useState("");
  const [clusterFilter, setClusterFilter] = useState("");
  const [range, setRange] = useState("all");
  const [customDays, setCustomDays] = useState({ fromDay: "", toDay: "" });
  const [bounds, setBounds] = useState({});
  const [actorChoices, setActorChoices] = useState([]);
  const [clusterNameById, setClusterNameById] = useState({});
  const [exporting, setExporting] = useState(false);
  const [actionError, setActionError] = useState("");
  // Only the newest request may write the list (filters change mid-flight).
  const requestRef = useRef(0);

  useEffect(() => {
    const timer = setTimeout(() => setActionFilter(actionInput.trim()), ACTION_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [actionInput]);

  // Date range, actor and action are filtered by the server, so they search
  // every entry, not just the loaded ones. Cluster is matched here (it is
  // read out of each entry's details), over what is loaded.
  const serverQuery = useMemo(
    () => ({
      from: bounds.from,
      to: bounds.to,
      actor: actorFilter || undefined,
      action: actionFilter || undefined,
    }),
    [bounds.from, bounds.to, actorFilter, actionFilter]
  );

  useEffect(() => {
    if (bounds.error) return;
    const id = ++requestRef.current;
    setLoading(true);
    listAuditLogs({ ...serverQuery, limit: PAGE_SIZE })
      .then((data) => {
        if (id !== requestRef.current) return;
        setEntries(data.items || []);
        setHasMore(Boolean(data.hasMore));
        setTotal(data.total ?? null);
        setError("");
      })
      .catch((err) => {
        if (id !== requestRef.current) return;
        setError(err.message || "Could not load the audit log.");
        setEntries([]);
        setHasMore(false);
        setTotal(null);
      })
      .finally(() => {
        if (id !== requestRef.current) return;
        setLoading(false);
        setReady(true);
      });
  }, [serverQuery, bounds.error]);

  useEffect(() => {
    // Who can be picked as actor: every user, not only those on the loaded page.
    listAuditActors()
      .then((data) => setActorChoices(data.items || []))
      .catch(() => {});
    // Resolve cluster IDs to names (best-effort; auditors may lack cluster view).
    listClusters()
      .then((clustersRes) => {
        const map = {};
        (clustersRes.items || []).forEach((c) => {
          if (c.id) map[c.id] = c.name || c.id;
        });
        setClusterNameById(map);
      })
      .catch(() => {
        // Ignore — fall back to IDs (and any names found in audit details).
      });
  }, []);

  // Paging goes by `hasMore` (exact); `total` is exact only on the last page.
  const loadMore = async () => {
    const id = requestRef.current;
    setLoadingMore(true);
    try {
      const data = await listAuditLogs({ ...serverQuery, limit: PAGE_SIZE, offset: entries.length });
      if (id !== requestRef.current) return;
      setEntries((prev) => {
        const seen = new Set(prev.map((entry) => entry.id));
        return [...prev, ...(data.items || []).filter((entry) => !seen.has(entry.id))];
      });
      setHasMore(Boolean(data.hasMore));
      setTotal(data.total ?? null);
    } catch (err) {
      setActionError(err.message || "Could not load older entries.");
    } finally {
      setLoadingMore(false);
    }
  };

  const pickRange = (key) => {
    setRange(key);
    if (key === "custom") {
      // Start the custom range on the last 7 days so it is never empty-handed.
      const days = customDays.fromDay || customDays.toDay
        ? customDays
        : { fromDay: localDay(new Date(Date.now() - 6 * 24 * 3600 * 1000)), toDay: localDay(new Date()) };
      setCustomDays(days);
      setBounds(rangeBounds("custom", days));
    } else {
      // A quick range counts back from now, each time it is picked.
      setBounds(rangeBounds(key));
    }
  };

  const changeCustomDay = (side, value) => {
    const days = { ...customDays, [side]: value };
    setCustomDays(days);
    setBounds(rangeBounds("custom", days));
  };

  const uniqueActors = useMemo(() => {
    const seen = new Set(actorChoices);
    entries.forEach((e) => seen.add(String(displayActor(e))));
    if (actorFilter) seen.add(actorFilter);
    // Automation first, then names A–Z.
    const rest = [...seen].filter((a) => a !== AUTOMATION_ACTOR).sort((a, b) => a.localeCompare(b));
    return [AUTOMATION_ACTOR, ...rest];
  }, [actorChoices, entries, actorFilter]);

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
    if (clusterFilter) seen.add(clusterFilter);
    return [...seen].sort((a, b) =>
      clusterLabel(a).localeCompare(clusterLabel(b))
    );
  }, [entries, clusterLabels, clusterFilter]);

  const filtered = useMemo(
    () => (clusterFilter ? entries.filter((e) => clusterOf(e) === clusterFilter) : entries),
    [entries, clusterFilter]
  );

  const anyFilter = Boolean(actorFilter || actionInput.trim() || clusterFilter || range !== "all");

  const clearFilters = () => {
    setActorFilter("");
    setActionInput("");
    setActionFilter("");
    setClusterFilter("");
    setRange("all");
    setBounds({});
  };

  const runExport = async () => {
    setExporting(true);
    setActionError("");
    try {
      const blob = await exportAuditLogs({
        from: bounds.from,
        to: bounds.to,
        actor: actorFilter,
        action: actionFilter,
        cluster: clusterFilter,
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      const scope = anyFilter ? "-filtered" : "";
      link.href = url;
      link.download = `audit-logs${scope}-${stamp}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setActionError(err.message || "Export failed.");
    } finally {
      setExporting(false);
    }
  };

  const scopeText = [
    describeRange(range, customDays),
    actorFilter ? `by ${actorFilter}` : "",
    actionFilter ? `matching “${actionFilter}”` : "",
  ].filter(Boolean).join(" ");
  const count = entries.length;
  const summary = hasMore
    ? `Showing the newest ${count.toLocaleString()} of about ${Math.max(total ?? 0, count).toLocaleString()} entries`
    : `${count.toLocaleString()} ${count === 1 ? "entry" : "entries"}`;

  const accessDenied = isAccessDeniedError(error);

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <h2>Audit Logs</h2>
        <p className="muted">Security-relevant actions across the control plane.</p>

        {ready && !accessDenied && (
          <>
            <div className="audit-range" role="group" aria-label="Time range">
              <span className="audit-range__label">When</span>
              <div className="settings-segmented">
                {QUICK_RANGES.map((option) => (
                  <button
                    key={option.key}
                    type="button"
                    aria-pressed={range === option.key}
                    onClick={() => pickRange(option.key)}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              {range === "custom" ? (
                <div className="audit-range__days">
                  <label>
                    From
                    <input
                      type="date"
                      value={customDays.fromDay}
                      max={customDays.toDay || undefined}
                      onChange={(e) => changeCustomDay("fromDay", e.target.value)}
                    />
                  </label>
                  <label>
                    To
                    <input
                      type="date"
                      value={customDays.toDay}
                      min={customDays.fromDay || undefined}
                      onChange={(e) => changeCustomDay("toDay", e.target.value)}
                    />
                  </label>
                  <span className="muted audit-range__hint">Whole days, in your local time.</span>
                </div>
              ) : null}
            </div>
            {bounds.error ? <p className="sg-zh-inline-error" role="alert">{bounds.error}</p> : null}

            <div className="user-filters audit-filters" style={{ marginBottom: "1rem" }}>
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
                  value={actionInput}
                  onChange={(e) => setActionInput(e.target.value)}
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
              {anyFilter && (
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  style={{ alignSelf: "flex-end" }}
                  onClick={clearFilters}
                >
                  Clear
                </button>
              )}
              <button
                type="button"
                className="btn-primary btn-compact"
                style={{ alignSelf: "flex-end" }}
                onClick={runExport}
                disabled={exporting || Boolean(bounds.error)}
                title={
                  anyFilter
                    ? "Export the entries matching the current filters as CSV"
                    : "Export all audit entries as CSV"
                }
              >
                {exporting ? "Exporting…" : anyFilter ? "Export filtered" : "Export CSV"}
              </button>
            </div>
          </>
        )}

        {actionError ? <ErrorBanner message={actionError} onDismiss={() => setActionError("")} /> : null}

        {!ready ? (
          <p className="muted">Loading audit logs...</p>
        ) : accessDenied ? (
          <AccessDeniedPage message={error} />
        ) : formatAccessError(error) ? (
          <ErrorBanner message={error} suppressAccessDenied={false} />
        ) : null}

        {ready && !accessDenied ? (
          <>
            <p className="muted" style={{ marginBottom: "0.5rem" }} aria-live="polite">
              {loading
                ? "Loading…"
                : `${summary}${scopeText ? ` ${scopeText}` : ""}.`}
              {!loading && clusterFilter
                ? ` ${filtered.length.toLocaleString()} of the loaded ones are on ${clusterLabel(clusterFilter)}${
                    hasMore ? " — the cluster filter searches what is loaded; Export covers all" : ""
                  }.`
                : ""}
            </p>
            <div className="table-wrap" aria-busy={loading} style={loading ? { opacity: 0.6 } : undefined}>
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
                      <td colSpan={5} className="muted">
                        {loading ? "Loading…" : anyFilter ? "No entries match the current filters." : "No audit entries yet."}
                      </td>
                    </tr>
                  ) : (
                    filtered.map((entry) => (
                      <tr key={entry.id}>
                        <td>{formatWhen(entry.createdAt)}</td>
                        <td>{displayActor(entry)}</td>
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
            {hasMore && !loading ? (
              <div style={{ display: "flex", justifyContent: "center", marginTop: "0.75rem" }}>
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  onClick={loadMore}
                  disabled={loadingMore}
                >
                  {loadingMore ? "Loading…" : "Load older entries"}
                </button>
              </div>
            ) : null}
          </>
        ) : null}
      </section>
    </div>
  );
}
