import { useCallback, useEffect, useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import {
  getZohoSourceClusters,
  getZohoSourceDeployments,
  getZohoSourceNamespaces,
  updateZohoSource,
} from "../../api/zohoApi.js";

/**
 * Modal to choose the dropdown source: a cluster + which of its namespaces feed
 * the Zoho Environment field, and — per namespace — exactly which live deployments
 * feed the Application field. Each namespace is either "All deployments" (dynamic:
 * future deployments auto-included) or an explicit subset. Saving persists the
 * source; the operator publishes it with "Sync now".
 */
export default function ZohoSourcePicker({
  initialClusterId = "",
  initialNamespaces = [],
  initialDeployments = {},
  onClose,
  onSaved,
}) {
  const [clusters, setClusters] = useState([]);
  const [clusterId, setClusterId] = useState(initialClusterId);
  const [namespaces, setNamespaces] = useState([]);
  const [selected, setSelected] = useState(() => new Set(initialNamespaces));
  const [groups, setGroups] = useState([]);
  // Per-namespace deployment selection: { [ns]: { all: bool, names: Set<string> } }
  const [deploySel, setDeploySel] = useState({});

  const [loadingClusters, setLoadingClusters] = useState(true);
  const [loadingNs, setLoadingNs] = useState(false);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // Search filters to tame long lists.
  const [nsFilter, setNsFilter] = useState("");
  const [depFilters, setDepFilters] = useState({}); // { [ns]: query }

  // 1) Load selectable clusters once.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoadingClusters(true);
      try {
        const res = await getZohoSourceClusters();
        if (!cancelled) setClusters(res.items || []);
      } catch (err) {
        if (!cancelled) setError(err.message || "Failed to load clusters.");
      } finally {
        if (!cancelled) setLoadingClusters(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 2) Load the chosen cluster's namespaces.
  const loadNamespaces = useCallback(async (id) => {
    if (!id) {
      setNamespaces([]);
      return;
    }
    setLoadingNs(true);
    setError("");
    try {
      const res = await getZohoSourceNamespaces(id);
      setNamespaces(res.namespaces || []);
    } catch (err) {
      setNamespaces([]);
      setError(err.message || "Failed to load namespaces for this cluster.");
    } finally {
      setLoadingNs(false);
    }
  }, []);

  useEffect(() => {
    if (clusterId) loadNamespaces(clusterId);
  }, [clusterId, loadNamespaces]);

  // 3) Preview the live deployments of the selected namespaces (auto-refresh),
  //    then reconcile per-namespace deployment selection (defaults to "all").
  useEffect(() => {
    const chosen = [...selected];
    if (!clusterId || chosen.length === 0) {
      setGroups([]);
      return;
    }
    let cancelled = false;
    setLoadingPreview(true);
    (async () => {
      try {
        const res = await getZohoSourceDeployments(clusterId, chosen);
        if (cancelled) return;
        const nextGroups = res.groups || [];
        setGroups(nextGroups);
        setDeploySel((prev) => {
          const next = {};
          for (const g of nextGroups) {
            const existing = prev[g.namespace];
            if (existing) {
              next[g.namespace] = existing;
            } else if (initialDeployments[g.namespace]) {
              const init = initialDeployments[g.namespace];
              const isAll = init.all !== false;
              next[g.namespace] = {
                all: isAll,
                names: new Set(isAll ? g.deployments || [] : init.names || []),
              };
            } else {
              // Default: all deployments (dynamic).
              next[g.namespace] = { all: true, names: new Set(g.deployments || []) };
            }
          }
          return next;
        });
      } catch (err) {
        if (!cancelled) setError(err.message || "Failed to preview deployments.");
      } finally {
        if (!cancelled) setLoadingPreview(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId, selected]);

  const changeCluster = (id) => {
    setClusterId(id);
    setSelected(new Set()); // namespaces belong to a cluster — reset on switch
    setGroups([]);
    setDeploySel({});
  };

  const toggleNamespace = (name) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const setAllForNs = (ns, all, allNames) => {
    setDeploySel((prev) => ({
      ...prev,
      [ns]: { all, names: new Set(all ? allNames : prev[ns]?.names || allNames) },
    }));
  };

  const toggleDeployment = (ns, name) => {
    setDeploySel((prev) => {
      const cur = prev[ns] || { all: true, names: new Set() };
      const names = new Set(cur.names);
      if (names.has(name)) names.delete(name);
      else names.add(name);
      // Editing individual boxes implies an explicit subset.
      return { ...prev, [ns]: { all: false, names } };
    });
  };

  // Bulk add/remove a set of names for a namespace (used by "Select shown" / "Clear").
  const bulkSetNames = (ns, names, add) => {
    setDeploySel((prev) => {
      const cur = prev[ns] || { all: false, names: new Set() };
      const next = new Set(cur.names);
      names.forEach((n) => (add ? next.add(n) : next.delete(n)));
      return { ...prev, [ns]: { all: false, names: next } };
    });
  };

  const filterDeps = (ns, deps) => {
    const q = (depFilters[ns] || "").trim().toLowerCase();
    return q ? deps.filter((d) => d.toLowerCase().includes(q)) : deps;
  };

  const nsSelectedCount = (g) => {
    const sel = deploySel[g.namespace];
    if (!sel || sel.all) return (g.deployments || []).length;
    return (g.deployments || []).filter((d) => sel.names.has(d)).length;
  };

  const totalSelected = groups.reduce((sum, g) => sum + nsSelectedCount(g), 0);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const deployments = {};
      for (const ns of selected) {
        const sel = deploySel[ns];
        if (!sel || sel.all) deployments[ns] = { all: true };
        else deployments[ns] = { all: false, names: [...sel.names] };
      }
      const data = await updateZohoSource({
        clusterId,
        namespaces: [...selected],
        deployments,
      });
      onSaved?.(data);
      onClose?.();
    } catch (err) {
      setError(err.message || "Failed to save the source.");
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel"
        role="dialog"
        aria-label="Choose dropdown source"
        onClick={(e) => e.stopPropagation()}
        style={{ width: "min(60rem, 100%)" }}
      >
        <header className="modal-header">
          <div>
            <h3>Choose namespaces &amp; deployments</h3>
            <p className="muted">
              Pick a cluster and the namespaces to publish as the <code>Environment</code> options. For
              each namespace, publish <strong>all</strong> live deployments (auto-includes future ones)
              or choose exactly which deployments feed the <code>Application</code> field.
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {/* Cluster ------------------------------------------------------- */}
          <div>
            <span
              style={{
                fontSize: "0.8rem",
                color: "var(--text-muted)",
                fontWeight: 600,
                display: "block",
                marginBottom: 4,
              }}
            >
              Cluster
            </span>
            <select
              value={clusterId}
              onChange={(e) => changeCluster(e.target.value)}
              disabled={loadingClusters}
              style={{ width: "100%" }}
            >
              <option value="">{loadingClusters ? "Loading clusters…" : "Select a cluster…"}</option>
              {clusters.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>

          {clusterId ? (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(200px, 260px) minmax(0, 1fr)",
                gap: 16,
                alignItems: "start",
              }}
            >
              {/* Namespaces column --------------------------------------- */}
              <div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                    gap: 8,
                    marginBottom: 6,
                  }}
                >
                  <strong style={{ fontSize: "0.9rem" }}>
                    Namespaces {selected.size ? `(${selected.size})` : ""}
                  </strong>
                  {namespaces.length ? (
                    <span style={{ display: "flex", gap: 10 }}>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => setSelected(new Set(namespaces))}
                      >
                        All
                      </button>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => setSelected(new Set())}
                      >
                        Clear
                      </button>
                    </span>
                  ) : null}
                </div>
                {loadingNs ? (
                  <p className="muted">Loading namespaces…</p>
                ) : namespaces.length === 0 ? (
                  <p className="muted">No namespaces found in this cluster.</p>
                ) : (
                  <>
                    {namespaces.length > 8 ? (
                      <input
                        type="text"
                        value={nsFilter}
                        onChange={(e) => setNsFilter(e.target.value)}
                        placeholder="Filter namespaces…"
                        style={{ width: "100%", marginBottom: 6 }}
                      />
                    ) : null}
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 2,
                        maxHeight: 340,
                        overflowY: "auto",
                        padding: 8,
                        border: "1px solid var(--border, #e5e7eb)",
                        borderRadius: 10,
                      }}
                    >
                      {namespaces
                        .filter((ns) => ns.toLowerCase().includes(nsFilter.trim().toLowerCase()))
                        .map((ns) => (
                          <label
                            key={ns}
                            className="checkbox-label"
                            style={{ margin: 0, padding: "3px 4px" }}
                          >
                            <input
                              type="checkbox"
                              checked={selected.has(ns)}
                              onChange={() => toggleNamespace(ns)}
                            />
                            <span className="mono">{ns}</span>
                          </label>
                        ))}
                    </div>
                  </>
                )}
              </div>

              {/* Deployments column -------------------------------------- */}
              <div style={{ minWidth: 0 }}>
                <strong style={{ fontSize: "0.9rem" }}>
                  Application deployments {loadingPreview ? "(loading…)" : `(${totalSelected} selected)`}
                </strong>
                <p className="field-hint" style={{ marginTop: 2, marginBottom: 8 }}>
                  These become the <code>Application</code> options, filtered per namespace by the cascade.
                </p>
                {selected.size === 0 ? (
                  <p className="muted">Select one or more namespaces to choose deployments.</p>
                ) : (
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: 10,
                      maxHeight: 360,
                      overflowY: "auto",
                      paddingRight: 4,
                    }}
                  >
                    {groups.map((g) => {
                      const sel = deploySel[g.namespace] || { all: true, names: new Set() };
                      const deps = g.deployments || [];
                      const shown = sel.all ? deps : filterDeps(g.namespace, deps);
                      const CHIP_CAP = 30;
                      return (
                        <div
                          key={g.namespace}
                          style={{
                            padding: "10px 12px",
                            border: "1px solid var(--border, #e5e7eb)",
                            borderRadius: 10,
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              gap: 8,
                              flexWrap: "wrap",
                            }}
                          >
                            <span className="mono" style={{ fontWeight: 600 }}>
                              {g.namespace}{" "}
                              <span className="muted" style={{ fontWeight: 400 }}>
                                ({nsSelectedCount(g)}/{deps.length})
                              </span>
                            </span>
                            <label
                              className="checkbox-label"
                              style={{ margin: 0, fontSize: "0.8rem" }}
                            >
                              <input
                                type="checkbox"
                                checked={sel.all}
                                disabled={deps.length === 0}
                                onChange={(e) => setAllForNs(g.namespace, e.target.checked, deps)}
                              />
                              All (include future)
                            </label>
                          </div>

                          {deps.length === 0 ? (
                            <div className="muted" style={{ marginTop: 8 }}>
                              no deployments
                            </div>
                          ) : sel.all ? (
                            <div className="chip-row" style={{ marginTop: 8 }}>
                              {deps.slice(0, CHIP_CAP).map((d) => (
                                <span key={d} className="badge status-muted mono">
                                  {d}
                                </span>
                              ))}
                              {deps.length > CHIP_CAP ? (
                                <span className="muted">+{deps.length - CHIP_CAP} more</span>
                              ) : null}
                            </div>
                          ) : (
                            <>
                              <div
                                style={{
                                  display: "flex",
                                  gap: 8,
                                  alignItems: "center",
                                  marginTop: 8,
                                  flexWrap: "wrap",
                                }}
                              >
                                <input
                                  type="text"
                                  value={depFilters[g.namespace] || ""}
                                  onChange={(e) =>
                                    setDepFilters((p) => ({ ...p, [g.namespace]: e.target.value }))
                                  }
                                  placeholder={`Filter ${deps.length} deployments…`}
                                  style={{ flex: "1 1 160px", minWidth: 0 }}
                                />
                                <button
                                  type="button"
                                  className="link-button"
                                  onClick={() => bulkSetNames(g.namespace, shown, true)}
                                >
                                  Select shown{shown.length !== deps.length ? ` (${shown.length})` : ""}
                                </button>
                                <button
                                  type="button"
                                  className="link-button"
                                  onClick={() => bulkSetNames(g.namespace, deps, false)}
                                >
                                  Clear
                                </button>
                              </div>
                              <div
                                style={{
                                  display: "grid",
                                  gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
                                  gap: 4,
                                  marginTop: 8,
                                  maxHeight: 220,
                                  overflowY: "auto",
                                }}
                              >
                                {shown.map((d) => (
                                  <label key={d} className="checkbox-label" style={{ margin: 0 }}>
                                    <input
                                      type="checkbox"
                                      checked={sel.names.has(d)}
                                      onChange={() => toggleDeployment(g.namespace, d)}
                                    />
                                    <span className="mono">{d}</span>
                                  </label>
                                ))}
                                {shown.length === 0 ? (
                                  <span className="muted">no matches</span>
                                ) : null}
                              </div>
                            </>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <p className="muted">Select a cluster to choose namespaces and deployments.</p>
          )}
        </div>

        <div className="modal-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            onClick={save}
            disabled={saving || !clusterId}
          >
            {saving ? "Saving…" : "Save source"}
          </button>
        </div>
      </section>
    </div>
  );
}
