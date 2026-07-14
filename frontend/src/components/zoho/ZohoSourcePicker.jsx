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
 * future deployments auto-included) or an explicit subset. Cluster targets can
 * carry Jenkins job overrides (a namespace — or specific deployments — builds via
 * its own job + parameter map instead of the router). Custom (non-cluster)
 * environments can be added too: a free-text Environment name (e.g. POS-UAT) with
 * free-text applications, plus the Jenkins job + parameter map its tickets should
 * trigger. Saving persists the source; the operator publishes it with "Sync now".
 */
export default function ZohoSourcePicker({
  initialClusterId = "",
  initialNamespaces = [],
  initialDeployments = {},
  initialCustom = [],
  initialOverrides = [],
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

  // Custom (non-cluster) environments. Params are kept as ordered rows for
  // editing and folded back into an object on save.
  const [custom, setCustom] = useState(() =>
    (initialCustom || []).map((c) => ({
      name: c.name || "",
      applications: [...(c.applications || [])],
      jenkinsJobPath: c.jenkinsJobPath || "",
      params: Object.entries(c.jenkinsParams || {}).map(([name, value]) => ({
        name,
        value: value == null ? "" : String(value),
      })),
    }))
  );
  const [newEnvName, setNewEnvName] = useState("");
  const [appDrafts, setAppDrafts] = useState({}); // { [envName]: text }

  // Jenkins job overrides for cluster targets. scope "all" = the whole
  // namespace (deployments saved as []); "some" = the listed deployments only.
  const [overrides, setOverrides] = useState(() =>
    (initialOverrides || []).map((o) => ({
      namespace: o.namespace || "",
      scope: (o.deployments || []).length ? "some" : "all",
      deployments: [...(o.deployments || [])],
      jenkinsJobPath: o.jenkinsJobPath || "",
      params: Object.entries(o.jenkinsParams || {}).map(([name, value]) => ({
        name,
        value: value == null ? "" : String(value),
      })),
    }))
  );

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

  // --- Custom environments ------------------------------------------------
  const addCustomEnv = () => {
    const name = newEnvName.trim();
    if (!name) return;
    const taken = new Set(
      [...custom.map((c) => c.name), ...namespaces].map((n) => n.toLowerCase())
    );
    if (taken.has(name.toLowerCase())) {
      setError(`"${name}" already exists as a namespace or custom environment.`);
      return;
    }
    setCustom((prev) => [
      ...prev,
      { name, applications: [], jenkinsJobPath: "", params: [] },
    ]);
    setNewEnvName("");
  };

  const removeCustomEnv = (idx) =>
    setCustom((prev) => prev.filter((_, i) => i !== idx));

  const patchCustom = (idx, patch) =>
    setCustom((prev) => prev.map((c, i) => (i === idx ? { ...c, ...patch } : c)));

  const addCustomApp = (idx) => {
    const env = custom[idx];
    const draft = (appDrafts[env.name] || "").trim();
    if (!draft) return;
    // Comma-separated input adds several at once.
    const incoming = draft.split(",").map((s) => s.trim()).filter(Boolean);
    const existing = new Set(env.applications.map((a) => a.toLowerCase()));
    const merged = [...env.applications];
    incoming.forEach((a) => {
      if (!existing.has(a.toLowerCase())) {
        existing.add(a.toLowerCase());
        merged.push(a);
      }
    });
    patchCustom(idx, { applications: merged });
    setAppDrafts((prev) => ({ ...prev, [env.name]: "" }));
  };

  const removeCustomApp = (idx, app) =>
    patchCustom(idx, {
      applications: custom[idx].applications.filter((a) => a !== app),
    });

  const addParamRow = (idx) =>
    patchCustom(idx, { params: [...custom[idx].params, { name: "", value: "" }] });

  const setParamRow = (idx, pIdx, field, value) =>
    patchCustom(idx, {
      params: custom[idx].params.map((p, i) => (i === pIdx ? { ...p, [field]: value } : p)),
    });

  const removeParamRow = (idx, pIdx) =>
    patchCustom(idx, { params: custom[idx].params.filter((_, i) => i !== pIdx) });

  // --- Jenkins job overrides (cluster targets) ------------------------------
  const addOverride = () => {
    const first = [...selected][0] || "";
    setOverrides((prev) => [
      ...prev,
      { namespace: first, scope: "all", deployments: [], jenkinsJobPath: "", params: [] },
    ]);
  };

  const removeOverride = (idx) => setOverrides((prev) => prev.filter((_, i) => i !== idx));

  const patchOverride = (idx, patch) =>
    setOverrides((prev) => prev.map((o, i) => (i === idx ? { ...o, ...patch } : o)));

  const addOverrideDeployment = (idx, name) => {
    if (!name) return;
    const cur = overrides[idx];
    if (cur.deployments.includes(name)) return;
    patchOverride(idx, { deployments: [...cur.deployments, name] });
  };

  const removeOverrideDeployment = (idx, name) =>
    patchOverride(idx, {
      deployments: overrides[idx].deployments.filter((d) => d !== name),
    });

  const addOverrideParamRow = (idx) =>
    patchOverride(idx, { params: [...overrides[idx].params, { name: "", value: "" }] });

  const setOverrideParamRow = (idx, pIdx, field, value) =>
    patchOverride(idx, {
      params: overrides[idx].params.map((p, i) => (i === pIdx ? { ...p, [field]: value } : p)),
    });

  const removeOverrideParamRow = (idx, pIdx) =>
    patchOverride(idx, { params: overrides[idx].params.filter((_, i) => i !== pIdx) });

  // The live deployments known for a namespace (loaded for selected namespaces).
  const namespaceDeployments = (ns) =>
    (groups.find((g) => g.namespace === ns) || {}).deployments || [];

  const nsSelectedCount = (g) => {
    const sel = deploySel[g.namespace];
    if (!sel || sel.all) return (g.deployments || []).length;
    return (g.deployments || []).filter((d) => sel.names.has(d)).length;
  };

  const totalSelected = groups.reduce((sum, g) => sum + nsSelectedCount(g), 0);

  const save = async () => {
    const incomplete = overrides.find(
      (o) => o.namespace && o.scope === "some" && o.deployments.length === 0
    );
    if (incomplete) {
      setError(
        `The Jenkins override on "${incomplete.namespace}" targets specific deployments ` +
          "but none are chosen — add one or switch it to the whole namespace."
      );
      return;
    }
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
        customEnvironments: custom
          .filter((c) => c.name.trim())
          .map((c) => ({
            name: c.name.trim(),
            applications: c.applications,
            jenkinsJobPath: c.jenkinsJobPath.trim(),
            jenkinsParams: Object.fromEntries(
              c.params
                .filter((p) => p.name.trim())
                .map((p) => [p.name.trim(), p.value])
            ),
          })),
        jobOverrides: overrides
          .filter((o) => o.namespace.trim())
          .map((o) => ({
            namespace: o.namespace.trim(),
            deployments: o.scope === "all" ? [] : o.deployments,
            jenkinsJobPath: o.jenkinsJobPath.trim(),
            jenkinsParams: Object.fromEntries(
              o.params
                .filter((p) => p.name.trim())
                .map((p) => [p.name.trim(), p.value])
            ),
          })),
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
        className="card modal-panel sg-zh-picker"
        role="dialog"
        aria-label="Choose dropdown source"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3>Choose namespaces &amp; deployments</h3>
            <p className="muted">
              Pick a cluster and the namespaces to publish as the <code>Environment</code> options. For
              each namespace, publish <strong>all</strong> live deployments (auto-includes future ones)
              or choose exactly which deployments feed the <code>Application</code> field. Targets that
              are <strong>not</strong> in the cluster (e.g. <code>POS-UAT</code>) go under Custom
              environments below.
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}

        {/* Cluster --------------------------------------------------------- */}
        <div className="sg-zh-pick-cluster">
          <span className="sg-zh-pick-label">Cluster</span>
          <select
            value={clusterId}
            onChange={(e) => changeCluster(e.target.value)}
            disabled={loadingClusters}
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
          <div className="sg-zh-pick-grid">
            {/* Namespaces column ------------------------------------------- */}
            <div>
              <div className="sg-zh-pick-head">
                <b>Namespaces {selected.size ? `(${selected.size})` : ""}</b>
                {namespaces.length ? (
                  <span className="sg-zh-pick-links">
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
                      className="sg-zh-pick-filter"
                      value={nsFilter}
                      onChange={(e) => setNsFilter(e.target.value)}
                      placeholder="Filter namespaces…"
                    />
                  ) : null}
                  <div className="sg-zh-pick-list">
                    {namespaces
                      .filter((ns) => ns.toLowerCase().includes(nsFilter.trim().toLowerCase()))
                      .map((ns) => (
                        <label key={ns} className="checkbox-label">
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

            {/* Deployments column ------------------------------------------ */}
            <div>
              <div className="sg-zh-pick-head">
                <b>
                  Application deployments{" "}
                  {loadingPreview ? "(loading…)" : `(${totalSelected} selected)`}
                </b>
              </div>
              <p className="field-hint">
                These become the <code>Application</code> options, filtered per namespace by the cascade.
              </p>
              {selected.size === 0 ? (
                <p className="muted">Select one or more namespaces to choose deployments.</p>
              ) : (
                <div className="sg-zh-pick-scroll">
                  {groups.map((g) => {
                    const sel = deploySel[g.namespace] || { all: true, names: new Set() };
                    const deps = g.deployments || [];
                    const shown = sel.all ? deps : filterDeps(g.namespace, deps);
                    const CHIP_CAP = 30;
                    return (
                      <div key={g.namespace} className="sg-zh-pick-group">
                        <div className="sg-zh-pick-group-head">
                          <span className="mono">
                            <b>{g.namespace}</b>{" "}
                            <span className="sg-zh-gcount">
                              ({nsSelectedCount(g)}/{deps.length})
                            </span>
                          </span>
                          <label className="checkbox-label">
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
                          <div className="sg-zh-fhint">no deployments</div>
                        ) : sel.all ? (
                          <div className="sg-zh-tags sg-zh-fvals">
                            {deps.slice(0, CHIP_CAP).map((d) => (
                              <span key={d} className="sg-tag">{d}</span>
                            ))}
                            {deps.length > CHIP_CAP ? (
                              <span className="sg-zh-more">+{deps.length - CHIP_CAP} more</span>
                            ) : null}
                          </div>
                        ) : (
                          <>
                            <div className="sg-zh-dep-tools">
                              <input
                                type="text"
                                value={depFilters[g.namespace] || ""}
                                onChange={(e) =>
                                  setDepFilters((p) => ({ ...p, [g.namespace]: e.target.value }))
                                }
                                placeholder={`Filter ${deps.length} deployments…`}
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
                            <div className="sg-zh-dep-grid">
                              {shown.map((d) => (
                                <label key={d} className="checkbox-label">
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

        {/* Jenkins job overrides (cluster targets) -------------------------- */}
        {clusterId ? (
          <div className="sg-zh-ovr">
            <div className="sg-zh-pick-head">
              <b>Jenkins job overrides {overrides.length ? `(${overrides.length})` : ""}</b>
              <button
                type="button"
                className="link-button"
                onClick={addOverride}
                disabled={selected.size === 0}
              >
                Add override
              </button>
            </div>
            <p className="field-hint">
              Send builds for chosen cluster targets to their <strong>own</strong> Jenkins job
              instead of the router. An override fires only when a build is needed (the ticket's
              image tag is missing from the registry); a rule for specific deployments beats a
              whole-namespace rule. Everything else — image check, deploy, pod health — is
              unchanged. Blank job path = the router job; no parameters = the standard
              APP/TAG/NAMESPACE contract. Parameter values may use <code>{"{app}"}</code>,{" "}
              <code>{"{tag}"}</code>, <code>{"{environment}"}</code> or any ticket field, e.g.{" "}
              <code>{"{cf_country}"}</code>.
            </p>
            {selected.size === 0 ? (
              <p className="muted">Select one or more namespaces to add overrides.</p>
            ) : null}

            {overrides.map((o, idx) => {
              const available = namespaceDeployments(o.namespace);
              const addable = available.filter((d) => !o.deployments.includes(d));
              const nsOptions = [...selected];
              if (o.namespace && !selected.has(o.namespace)) nsOptions.unshift(o.namespace);
              return (
                <div key={idx} className="sg-zh-pick-group sg-zh-custom-group">
                  <div className="sg-zh-pick-group-head">
                    <span className="mono">
                      <b>{o.namespace || "(pick a namespace)"}</b>{" "}
                      <span className="sg-zh-gcount">
                        {o.scope === "all"
                          ? "whole namespace"
                          : `${o.deployments.length} deployment${o.deployments.length === 1 ? "" : "s"}`}
                        {" → "}
                        {o.jenkinsJobPath.trim() || "router job"}
                      </span>
                    </span>
                    <button type="button" className="link-button" onClick={() => removeOverride(idx)}>
                      Remove
                    </button>
                  </div>

                  <div className="sg-zh-ovr-target">
                    <label>
                      <span>Namespace</span>
                      <select
                        value={o.namespace}
                        onChange={(e) =>
                          patchOverride(idx, { namespace: e.target.value, deployments: [] })
                        }
                      >
                        {!o.namespace ? <option value="">Pick a namespace…</option> : null}
                        {nsOptions.map((ns) => (
                          <option key={ns} value={ns}>
                            {ns}
                            {!selected.has(ns) ? " (no longer selected)" : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>Applies to</span>
                      <select
                        value={o.scope}
                        onChange={(e) => patchOverride(idx, { scope: e.target.value })}
                      >
                        <option value="all">Whole namespace</option>
                        <option value="some">Specific deployments</option>
                      </select>
                    </label>
                  </div>

                  {o.scope === "some" ? (
                    <>
                      <div className="sg-zh-tags sg-zh-fvals">
                        {o.deployments.map((d) => (
                          <span key={d} className="sg-tag sg-zh-chip">
                            {d}
                            <button
                              type="button"
                              className="sg-zh-chip-x"
                              onClick={() => removeOverrideDeployment(idx, d)}
                              aria-label={`Remove ${d}`}
                            >
                              ✕
                            </button>
                          </span>
                        ))}
                        {o.deployments.length === 0 ? (
                          <span className="muted">no deployments chosen yet</span>
                        ) : null}
                      </div>
                      <div className="sg-zh-ovr-add">
                        <select
                          value=""
                          disabled={!o.namespace || addable.length === 0}
                          onChange={(e) => addOverrideDeployment(idx, e.target.value)}
                        >
                          <option value="">
                            {!o.namespace
                              ? "Pick a namespace first…"
                              : addable.length === 0
                                ? "No more deployments"
                                : `Add deployment (${addable.length})…`}
                          </option>
                          {addable.map((d) => (
                            <option key={d} value={d}>
                              {d}
                            </option>
                          ))}
                        </select>
                      </div>
                    </>
                  ) : null}

                  <div className="sg-zh-custom-jenkins">
                    <label className="sg-zh-custom-field">
                      <span>Jenkins job path (blank = the router job)</span>
                      <input
                        type="text"
                        value={o.jenkinsJobPath}
                        onChange={(e) => patchOverride(idx, { jenkinsJobPath: e.target.value })}
                        placeholder="e.g. persona-deploy or folder/persona-deploy"
                      />
                    </label>
                    <div className="sg-zh-custom-params-head">
                      <span>
                        Build parameters{" "}
                        <span className="muted">(none = the router contract APP/TAG/NAMESPACE)</span>
                      </span>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => addOverrideParamRow(idx)}
                      >
                        Add parameter
                      </button>
                    </div>
                    {o.params.map((p, pIdx) => (
                      <div key={pIdx} className="sg-zh-custom-param">
                        <input
                          type="text"
                          value={p.name}
                          onChange={(e) => setOverrideParamRow(idx, pIdx, "name", e.target.value)}
                          placeholder="name (e.g. msName)"
                        />
                        <input
                          type="text"
                          value={p.value}
                          onChange={(e) => setOverrideParamRow(idx, pIdx, "value", e.target.value)}
                          placeholder="value (e.g. {app}, {tag}, uat)"
                        />
                        <button
                          type="button"
                          className="link-button"
                          onClick={() => removeOverrideParamRow(idx, pIdx)}
                          aria-label="Remove parameter"
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}

        {/* Custom (non-cluster) environments -------------------------------- */}
        <div className="sg-zh-custom">
          <div className="sg-zh-pick-head">
            <b>Custom environments {custom.length ? `(${custom.length})` : ""}</b>
          </div>
          <p className="field-hint">
            Extra <code>Environment</code> options that are <strong>not</strong> cluster namespaces,
            each with free-text applications for the <code>Application</code> cascade. Tickets for
            these skip the cluster entirely — KubeSight triggers the Jenkins job below with your
            parameters and the build result is the outcome. Parameter values may use{" "}
            <code>{"{app}"}</code>, <code>{"{tag}"}</code>, <code>{"{environment}"}</code> or any
            ticket field, e.g. <code>{"{cf_country}"}</code>.
          </p>
          <div className="sg-zh-custom-add">
            <input
              type="text"
              value={newEnvName}
              onChange={(e) => setNewEnvName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addCustomEnv();
                }
              }}
              placeholder='New environment name (e.g. "POS-UAT")…'
            />
            <button
              type="button"
              className="secondary"
              onClick={addCustomEnv}
              disabled={!newEnvName.trim()}
            >
              Add environment
            </button>
          </div>

          {custom.map((env, idx) => (
            <div key={env.name} className="sg-zh-pick-group sg-zh-custom-group">
              <div className="sg-zh-pick-group-head">
                <span className="mono">
                  <b>{env.name}</b>{" "}
                  <span className="sg-zh-gcount">
                    ({env.applications.length} application{env.applications.length === 1 ? "" : "s"})
                  </span>
                </span>
                <button type="button" className="link-button" onClick={() => removeCustomEnv(idx)}>
                  Remove
                </button>
              </div>

              <div className="sg-zh-tags sg-zh-fvals">
                {env.applications.map((app) => (
                  <span key={app} className="sg-tag sg-zh-chip">
                    {app}
                    <button
                      type="button"
                      className="sg-zh-chip-x"
                      onClick={() => removeCustomApp(idx, app)}
                      aria-label={`Remove ${app}`}
                    >
                      ✕
                    </button>
                  </span>
                ))}
                {env.applications.length === 0 ? (
                  <span className="muted">no applications yet</span>
                ) : null}
              </div>
              <div className="sg-zh-custom-add">
                <input
                  type="text"
                  value={appDrafts[env.name] || ""}
                  onChange={(e) =>
                    setAppDrafts((prev) => ({ ...prev, [env.name]: e.target.value }))
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addCustomApp(idx);
                    }
                  }}
                  placeholder="Add application (e.g. pos) — comma-separate for several…"
                />
                <button type="button" className="link-button" onClick={() => addCustomApp(idx)}>
                  Add
                </button>
              </div>

              <div className="sg-zh-custom-jenkins">
                <label className="sg-zh-custom-field">
                  <span>Jenkins job path (blank = the router job)</span>
                  <input
                    type="text"
                    value={env.jenkinsJobPath}
                    onChange={(e) => patchCustom(idx, { jenkinsJobPath: e.target.value })}
                    placeholder="e.g. pos-deploy or folder/pos-deploy"
                  />
                </label>
                <div className="sg-zh-custom-params-head">
                  <span>
                    Build parameters{" "}
                    <span className="muted">(none = the router contract APP/TAG/NAMESPACE)</span>
                  </span>
                  <button type="button" className="link-button" onClick={() => addParamRow(idx)}>
                    Add parameter
                  </button>
                </div>
                {env.params.map((p, pIdx) => (
                  <div key={pIdx} className="sg-zh-custom-param">
                    <input
                      type="text"
                      value={p.name}
                      onChange={(e) => setParamRow(idx, pIdx, "name", e.target.value)}
                      placeholder="name (e.g. repotag)"
                    />
                    <input
                      type="text"
                      value={p.value}
                      onChange={(e) => setParamRow(idx, pIdx, "value", e.target.value)}
                      placeholder="value (e.g. {tag}, uat, {cf_country})"
                    />
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => removeParamRow(idx, pIdx)}
                      aria-label="Remove parameter"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div className="modal-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            onClick={save}
            disabled={saving || (!clusterId && custom.length === 0)}
          >
            {saving ? "Saving…" : "Save source"}
          </button>
        </div>
      </section>
    </div>
  );
}
