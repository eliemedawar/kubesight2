import { useEffect, useMemo, useRef, useState } from "react";
import { listClients } from "../../api";
import {
  buildBlueprintDeployPlan,
  deployFromBlueprint,
  getServiceBlueprint,
  pickClusterResources,
  pickNamespaces,
  pickNamespacedResources,
} from "../../api/serviceBlueprintsApi.js";

const ENVIRONMENTS = ["dev", "staging", "production", "custom"];

// Logical kind -> namespaced picker kind for the "use existing" dropdown.
const KIND_TO_PICKER = {
  Deployment: "deployments",
  StatefulSet: "statefulsets",
  DaemonSet: "daemonsets",
  CronJob: "cronjobs",
  Service: "services",
};

const MAPPING_LABEL = {
  create_new: "Create new",
  existing_resource: "Use existing",
  external_dependency: "External",
  skip: "Skip",
};

const STEPS = ["Target", "Mapping", "Values", "Review", "Deploy"];

function StepHeader({ step }) {
  return (
    <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1rem", flexWrap: "wrap" }}>
      {STEPS.map((label, i) => (
        <span
          key={label}
          className={`status-badge status-badge--${i === step ? "pass" : i < step ? "pending" : "pending"}`}
          style={{ opacity: i <= step ? 1 : 0.5 }}
        >
          {i + 1}. {label}
        </span>
      ))}
    </div>
  );
}

export default function DeployFromBlueprintWizard({ blueprintId, blueprintName, clusters = [], onClose, onDeployed }) {
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [clients, setClients] = useState([]);
  const [blueprint, setBlueprint] = useState(null);
  const [plan, setPlan] = useState(null);
  const [result, setResult] = useState(null);

  const [target, setTarget] = useState({
    clientId: "", environment: "production", customEnvironment: "",
    clusterId: clusters[0]?.id || "", namespace: "", name: "",
  });
  const [namespaceOptions, setNamespaceOptions] = useState([]);
  const [creatingNamespace, setCreatingNamespace] = useState(false);
  const [nameEdited, setNameEdited] = useState(false);
  const [mappings, setMappings] = useState({}); // componentId -> {mappingType, generatedName, name, externalEndpoint, kind}
  const [requirementValues, setRequirementValues] = useState({}); // requirementId -> value

  const pickerCache = useRef({}); // `${cluster}:${ns}:${kind}` -> [names]
  const nsCache = useRef({}); // `${cluster}` -> [namespace names]
  const [pickerVersion, setPickerVersion] = useState(0);

  const effectiveEnv = target.environment === "custom"
    ? (target.customEnvironment.trim() || "custom")
    : target.environment;

  // Load clients + blueprint connections on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [clientsRes, bp] = await Promise.all([
          listClients().catch(() => ({ items: [] })),
          getServiceBlueprint(blueprintId),
        ]);
        if (cancelled) return;
        setClients(clientsRes.items || []);
        setBlueprint(bp);
      } catch (err) {
        if (!cancelled) setError(err.message || "Failed to load wizard data.");
      }
    })();
    return () => { cancelled = true; };
  }, [blueprintId]);

  // Fetch namespaces when the cluster changes.
  useEffect(() => {
    if (!target.clusterId) { setNamespaceOptions([]); return; }
    let cancelled = false;
    setCreatingNamespace(false);
    pickNamespaces(target.clusterId)
      .then((res) => { if (!cancelled) setNamespaceOptions(res.items || []); })
      .catch(() => { if (!cancelled) setNamespaceOptions([]); });
    return () => { cancelled = true; };
  }, [target.clusterId]);

  const setTargetField = (key, value) => setTarget((prev) => ({ ...prev, [key]: value }));

  // Step 1 -> 2: build the plan and seed mappings + requirement defaults.
  const goToMapping = async () => {
    if (!target.clusterId) { setError("Select a cluster."); return; }
    setBusy(true);
    setError("");
    try {
      const built = await buildBlueprintDeployPlan(blueprintId, {
        clientId: target.clientId || undefined,
        environment: effectiveEnv,
        clusterId: target.clusterId,
        namespace: target.namespace || undefined,
        name: target.name || undefined,
      });
      setPlan(built);
      if (!target.namespace && built.namespace) setTargetField("namespace", built.namespace);
      if (!target.name && built.appServiceName) setTargetField("name", built.appServiceName);

      const seededMappings = {};
      built.components.forEach((c) => {
        seededMappings[c.componentId] = {
          mappingType: c.recommendedMappingType,
          generatedName: c.generatedName,
          name: "",
          externalEndpoint: "",
          kind: c.kind,
          existingClusterId: target.clusterId,
          existingNamespace: target.namespace,
        };
      });
      setMappings(seededMappings);

      const seededValues = {};
      built.missingValues.forEach((m) => { seededValues[m.requirementId] = ""; });
      setRequirementValues(seededValues);

      setStep(1);
    } catch (err) {
      setError(err.message || "Failed to build deploy plan.");
    } finally {
      setBusy(false);
    }
  };

  const setMapping = (componentId, patch) =>
    setMappings((prev) => ({ ...prev, [componentId]: { ...prev[componentId], ...patch } }));

  // Lazily load namespaces for a (possibly different) cluster.
  const ensureNamespaces = async (clusterId) => {
    if (!clusterId || nsCache.current[clusterId] !== undefined) return;
    nsCache.current[clusterId] = []; // mark in-flight
    try {
      const res = await pickNamespaces(clusterId);
      nsCache.current[clusterId] = res.items || [];
    } catch {
      nsCache.current[clusterId] = [];
    }
    setPickerVersion((v) => v + 1);
  };

  // Lazily load existing-resource options for a kind in a cluster/namespace.
  const ensurePicker = async (clusterId, namespace, kind) => {
    const pickerKind = KIND_TO_PICKER[kind];
    if (!pickerKind || !clusterId || !namespace) return;
    const cacheKey = `${clusterId}:${namespace}:${pickerKind}`;
    if (pickerCache.current[cacheKey] !== undefined) return;
    pickerCache.current[cacheKey] = []; // mark in-flight
    try {
      const res = await pickNamespacedResources(clusterId, namespace, pickerKind);
      pickerCache.current[cacheKey] = res.items || [];
    } catch {
      pickerCache.current[cacheKey] = [];
    }
    setPickerVersion((v) => v + 1);
  };

  const resourceOptionsFor = (clusterId, namespace, kind) => {
    const pickerKind = KIND_TO_PICKER[kind];
    return pickerCache.current[`${clusterId}:${namespace}:${pickerKind}`] || [];
  };

  const ensureSecretPicker = async (kind) => {
    const cacheKey = kind === "tls" ? "secrets:tls" : "secrets";
    if (pickerCache.current[cacheKey] !== undefined) return;
    pickerCache.current[cacheKey] = [];
    try {
      const res = await pickNamespacedResources(
        target.clusterId, target.namespace, "secrets", kind === "tls" ? "tls" : undefined
      );
      pickerCache.current[cacheKey] = res.items || [];
    } catch {
      pickerCache.current[cacheKey] = [];
    }
    setPickerVersion((v) => v + 1);
  };

  const ensureConfigmapPicker = async () => {
    if (pickerCache.current.configmaps !== undefined) return;
    pickerCache.current.configmaps = [];
    try {
      const res = await pickNamespacedResources(target.clusterId, target.namespace, "configmaps");
      pickerCache.current.configmaps = res.items || [];
    } catch {
      pickerCache.current.configmaps = [];
    }
    setPickerVersion((v) => v + 1);
  };

  const componentName = (id) => blueprint?.components.find((c) => c.id === id)?.name || `#${id}`;

  const buildMappingsPayload = () =>
    (plan?.components || []).map((c) => {
      const m = mappings[c.componentId] || {};
      const isExisting = m.mappingType === "existing_resource";
      return {
        componentId: c.componentId,
        mappingType: m.mappingType,
        kind: c.kind,
        generatedName: m.generatedName || c.generatedName,
        name: isExisting ? m.name : undefined,
        // Existing resources may live in a different cluster/namespace than the
        // deploy target; record where it actually is so runtime topology resolves.
        clusterId: isExisting ? (m.existingClusterId || target.clusterId) : undefined,
        namespace: isExisting ? (m.existingNamespace || target.namespace) : undefined,
        externalEndpoint: m.mappingType === "external_dependency" ? m.externalEndpoint : undefined,
        labels: c.labels,
      };
    });

  const warnings = useMemo(() => {
    const w = [];
    (plan?.missingValues || []).forEach((m) => {
      if (!String(requirementValues[m.requirementId] || "").trim()) {
        w.push(`Missing required value: ${m.key}`);
      }
    });
    (plan?.components || []).forEach((c) => {
      const m = mappings[c.componentId];
      if (m?.mappingType === "existing_resource" && !String(m.name || "").trim()) {
        w.push(`${c.name}: no existing resource selected`);
      }
      if (m?.mappingType === "external_dependency" && !String(m.externalEndpoint || "").trim()) {
        w.push(`${c.name}: no external endpoint provided`);
      }
    });
    return w;
  }, [plan, mappings, requirementValues]);

  const deploy = async () => {
    setBusy(true);
    setError("");
    try {
      const appService = await deployFromBlueprint(blueprintId, {
        name: target.name || undefined,
        clientId: target.clientId || undefined,
        environment: effectiveEnv,
        clusterId: target.clusterId,
        namespace: target.namespace || undefined,
        mappings: buildMappingsPayload(),
        requirementValues,
      });
      setResult(appService);
      setStep(4);
      onDeployed?.(appService);
    } catch (err) {
      setError(err.message || "Deploy failed.");
    } finally {
      setBusy(false);
    }
  };

  // ---- step renderers -----------------------------------------------------

  const renderTarget = () => (
    <section className="form-section">
      <h4>Deployment target</h4>
      <div className="form-grid">
        <label className="form-grid__full">
          App service name
          <input
            value={target.name}
            placeholder="auto-generated from blueprint + environment"
            onChange={(e) => { setNameEdited(true); setTargetField("name", e.target.value); }}
          />
          <span className="muted" style={{ fontSize: "0.75rem" }}>
            Must be unique. Auto-updates with the environment until you edit it.
          </span>
        </label>
        <label>
          Client
          <select value={target.clientId} onChange={(e) => setTargetField("clientId", e.target.value)}>
            <option value="">— None —</option>
            {clients.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <label>
          Environment
          <select
            value={target.environment}
            onChange={(e) => {
              setTarget((prev) => ({
                ...prev,
                environment: e.target.value,
                // Refresh the auto-generated name/namespace unless the user edited them.
                name: nameEdited ? prev.name : "",
              }));
            }}
          >
            {ENVIRONMENTS.map((e) => <option key={e} value={e}>{e}</option>)}
          </select>
        </label>
        {target.environment === "custom" && (
          <label>
            Custom environment
            <input value={target.customEnvironment} placeholder="e.g. qa"
              onChange={(e) => setTarget((prev) => ({
                ...prev,
                customEnvironment: e.target.value,
                name: nameEdited ? prev.name : "",
              }))} />
          </label>
        )}
        <label>
          Cluster *
          <select value={target.clusterId} onChange={(e) => setTargetField("clusterId", e.target.value)}>
            <option value="">— Select —</option>
            {clusters.map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
          </select>
        </label>
        <label className="form-grid__full">
          Namespace
          {namespaceOptions.length > 0 && !creatingNamespace ? (
            <select
              value={target.namespace}
              onChange={(e) => {
                if (e.target.value === "__create__") {
                  setCreatingNamespace(true);
                  setTargetField("namespace", "");
                } else {
                  setTargetField("namespace", e.target.value);
                }
              }}
            >
              <option value="">Auto-suggest from client + environment</option>
              {namespaceOptions.map((ns) => <option key={ns} value={ns}>{ns}</option>)}
              <option value="__create__">+ Create new namespace…</option>
            </select>
          ) : (
            <input
              value={target.namespace}
              placeholder="auto-suggested if left blank"
              onChange={(e) => setTargetField("namespace", e.target.value)}
            />
          )}
          <span className="muted" style={{ fontSize: "0.75rem" }}>
            {namespaceOptions.length > 0 && !creatingNamespace
              ? "Pick an existing namespace, or choose “Create new”."
              : "Type a namespace to create, or leave blank to auto-suggest."}
            {creatingNamespace && (
              <>
                {" "}
                <button
                  type="button"
                  className="link-button"
                  onClick={() => { setCreatingNamespace(false); setTargetField("namespace", ""); }}
                  style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: "var(--accent, #2563eb)" }}
                >
                  Choose existing instead
                </button>
              </>
            )}
          </span>
        </label>
      </div>
    </section>
  );

  const renderMapping = () => (
    <section className="form-section">
      <h4>Component mapping</h4>
      <p className="muted" style={{ fontSize: "0.8rem" }}>
        Recommended choices are preselected. Adjust only what you need.
      </p>
      {(plan?.components || []).map((c) => {
        const m = mappings[c.componentId] || {};
        const supportsPicker = Boolean(KIND_TO_PICKER[c.kind]);
        const mCluster = m.existingClusterId || target.clusterId;
        const mNamespace = m.existingNamespace || "";
        const nsOptions = nsCache.current[mCluster] || [];
        const resourceOptions = resourceOptionsFor(mCluster, mNamespace, c.kind);
        return (
          <div key={c.componentId} className="card" style={{ padding: "0.85rem", marginTop: "0.6rem" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.5rem" }}>
              <strong>{c.name}</strong>
              <span className="chip">{c.componentType}</span>
              {c.optional && <span className="muted" style={{ fontSize: "0.75rem" }}>optional</span>}
              {c.recommendedMappingType && (
                <span className="muted" style={{ fontSize: "0.75rem", marginLeft: "auto" }}>
                  recommended: {MAPPING_LABEL[c.recommendedMappingType]}
                </span>
              )}
            </div>
            <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap", marginBottom: "0.5rem" }}>
              {c.options.map((opt) => (
                <label key={opt} className="checkbox-row" style={{ fontSize: "0.8rem" }}>
                  <input type="radio" name={`map-${c.componentId}`} checked={m.mappingType === opt}
                    onChange={() => {
                      setMapping(c.componentId, { mappingType: opt });
                      if (opt === "existing_resource") {
                        ensureNamespaces(mCluster);
                        if (mNamespace) ensurePicker(mCluster, mNamespace, c.kind);
                      }
                    }} />
                  {MAPPING_LABEL[opt]}
                </label>
              ))}
            </div>
            {m.mappingType === "create_new" && (
              <label>
                New resource name ({c.kind})
                <input value={m.generatedName || ""}
                  onChange={(e) => setMapping(c.componentId, { generatedName: e.target.value })} />
              </label>
            )}
            {m.mappingType === "existing_resource" && (
              <div className="form-grid">
                <label>
                  Cluster
                  <select
                    value={mCluster}
                    onChange={(e) => {
                      const cluster = e.target.value;
                      setMapping(c.componentId, { existingClusterId: cluster, existingNamespace: "", name: "" });
                      ensureNamespaces(cluster);
                    }}
                  >
                    {clusters.map((cl) => <option key={cl.id} value={cl.id}>{cl.name || cl.id}</option>)}
                  </select>
                </label>
                <label>
                  Namespace
                  {nsOptions.length > 0 ? (
                    <select
                      value={mNamespace}
                      onChange={(e) => {
                        const ns = e.target.value;
                        setMapping(c.componentId, { existingNamespace: ns, name: "" });
                        if (ns) ensurePicker(mCluster, ns, c.kind);
                      }}
                    >
                      <option value="">— Select namespace —</option>
                      {nsOptions.map((ns) => <option key={ns} value={ns}>{ns}</option>)}
                    </select>
                  ) : (
                    <input
                      value={mNamespace}
                      placeholder="namespace"
                      onChange={(e) => setMapping(c.componentId, { existingNamespace: e.target.value, name: "" })}
                      onBlur={(e) => e.target.value && ensurePicker(mCluster, e.target.value, c.kind)}
                    />
                  )}
                </label>
                <label className="form-grid__full">
                  Existing {c.kind}
                  {supportsPicker && resourceOptions.length > 0 ? (
                    <select value={m.name || ""} onChange={(e) => setMapping(c.componentId, { name: e.target.value })}>
                      <option value="">— Select {c.kind.toLowerCase()} —</option>
                      {resourceOptions.map((name) => <option key={name} value={name}>{name}</option>)}
                    </select>
                  ) : (
                    <input
                      value={m.name || ""}
                      placeholder={mNamespace ? "no resources found — type a name" : "select a namespace first"}
                      onChange={(e) => setMapping(c.componentId, { name: e.target.value })}
                    />
                  )}
                </label>
              </div>
            )}
            {m.mappingType === "external_dependency" && (
              <label>
                External endpoint
                <input value={m.externalEndpoint || ""} placeholder="db.company.com:5432"
                  onChange={(e) => setMapping(c.componentId, { externalEndpoint: e.target.value })} />
              </label>
            )}
          </div>
        );
      })}
    </section>
  );

  const renderValues = () => {
    const missing = plan?.missingValues || [];
    return (
      <section className="form-section">
        <h4>Required values</h4>
        {missing.length === 0 ? (
          <p className="muted">Nothing to fill — all values are auto-filled or generated.</p>
        ) : (
          <div className="form-grid">
            {missing.map((mv) => {
              const usesSecret = mv.valueSource === "existing_secret" || mv.requirementType === "tls_secret";
              const usesConfigmap = mv.valueSource === "existing_configmap";
              const datalistId = `req-${mv.requirementId}`;
              let options = [];
              if (usesSecret) {
                const key = mv.requirementType === "tls_secret" ? "secrets:tls" : "secrets";
                options = pickerCache.current[key] || [];
              } else if (usesConfigmap) {
                options = pickerCache.current.configmaps || [];
              }
              return (
                <label key={mv.requirementId} className="form-grid__full">
                  {mv.key} {mv.secret && <span className="status-badge status-badge--warning">secret</span>}
                  {Array.isArray(mv.allowedValues) && mv.allowedValues.length > 0 ? (
                    <select value={requirementValues[mv.requirementId] || ""}
                      onChange={(e) => setRequirementValues((p) => ({ ...p, [mv.requirementId]: e.target.value }))}>
                      <option value="">— Select —</option>
                      {mv.allowedValues.map((v) => <option key={v} value={v}>{v}</option>)}
                    </select>
                  ) : (
                    <>
                      <input
                        list={(usesSecret || usesConfigmap) ? datalistId : undefined}
                        type={mv.secret && !usesSecret ? "password" : "text"}
                        value={requirementValues[mv.requirementId] || ""}
                        placeholder={mv.description || mv.requirementType}
                        onFocus={() => {
                          if (mv.requirementType === "tls_secret") ensureSecretPicker("tls");
                          else if (usesSecret) ensureSecretPicker("all");
                          else if (usesConfigmap) ensureConfigmapPicker();
                        }}
                        onChange={(e) => setRequirementValues((p) => ({ ...p, [mv.requirementId]: e.target.value }))}
                      />
                      {(usesSecret || usesConfigmap) && (
                        <datalist id={datalistId}>
                          {options.map((name) => <option key={name} value={name} />)}
                        </datalist>
                      )}
                    </>
                  )}
                  {mv.description && <span className="muted" style={{ fontSize: "0.75rem" }}>{mv.description}</span>}
                </label>
              );
            })}
          </div>
        )}
      </section>
    );
  };

  const renderReview = () => {
    const comps = plan?.components || [];
    const group = (type) => comps.filter((c) => (mappings[c.componentId]?.mappingType) === type);
    const resolvedName = (c) => {
      const m = mappings[c.componentId] || {};
      if (m.mappingType === "create_new") return `${c.kind.toLowerCase()}/${m.generatedName || c.generatedName}`;
      if (m.mappingType === "existing_resource") {
        const base = `${c.kind.toLowerCase()}/${m.name || "?"}`;
        const cluster = m.existingClusterId || target.clusterId;
        const ns = m.existingNamespace || target.namespace;
        const crossCluster = cluster && cluster !== target.clusterId;
        return crossCluster ? `${base} @ ${cluster}/${ns}` : base;
      }
      if (m.mappingType === "external_dependency") return m.externalEndpoint || "external";
      return "skipped";
    };
    return (
      <section className="form-section">
        <h4>Review</h4>
        <div className="muted" style={{ fontSize: "0.85rem", marginBottom: "0.75rem" }}>
          <div><strong>{target.name}</strong></div>
          <div>
            {plan?.clientName ? `${plan.clientName} · ` : ""}{effectiveEnv} · {target.clusterId} · ns:{target.namespace}
          </div>
        </div>

        {/* Runtime topology chain from blueprint connections. */}
        {(blueprint?.connections || []).length > 0 && (
          <div style={{ marginBottom: "0.75rem" }}>
            <p className="form-label">Runtime topology</p>
            {blueprint.connections.map((cn) => {
              const src = comps.find((c) => c.componentId === cn.sourceComponentId);
              const tgt = comps.find((c) => c.componentId === cn.targetComponentId);
              const srcSkip = mappings[cn.sourceComponentId]?.mappingType === "skip";
              const tgtSkip = mappings[cn.targetComponentId]?.mappingType === "skip";
              if (srcSkip || tgtSkip) return null;
              return (
                <div key={cn.id} className="muted" style={{ fontSize: "0.82rem" }}>
                  {src ? resolvedName(src) : componentName(cn.sourceComponentId)} →{" "}
                  {tgt ? resolvedName(tgt) : componentName(cn.targetComponentId)}
                </div>
              );
            })}
          </div>
        )}

        {[
          ["create_new", "New resources"],
          ["existing_resource", "Linked existing"],
          ["external_dependency", "External dependencies"],
          ["skip", "Skipped"],
        ].map(([type, label]) => {
          const items = group(type);
          if (!items.length) return null;
          return (
            <div key={type} style={{ marginBottom: "0.5rem" }}>
              <p className="form-label" style={{ marginBottom: "0.25rem" }}>{label} ({items.length})</p>
              {items.map((c) => (
                <div key={c.componentId} style={{ fontSize: "0.82rem" }}>
                  {c.name}: <code>{resolvedName(c)}</code>
                </div>
              ))}
            </div>
          );
        })}

        {plan?.baseLabels && (
          <div style={{ marginTop: "0.5rem" }}>
            <p className="form-label" style={{ marginBottom: "0.25rem" }}>Labels</p>
            {Object.entries(plan.baseLabels).map(([k, v]) => (
              <div key={k} className="muted" style={{ fontSize: "0.78rem" }}><code>{k}={v}</code></div>
            ))}
          </div>
        )}

        {warnings.length > 0 && (
          <div className="banner-message error" style={{ marginTop: "0.75rem" }}>
            <strong>Warnings</strong>
            <ul style={{ margin: "0.25rem 0 0", paddingLeft: "1.1rem" }}>
              {warnings.map((w) => <li key={w}>{w}</li>)}
            </ul>
          </div>
        )}
      </section>
    );
  };

  const renderDone = () => (
    <section className="form-section">
      <h4>Deployed</h4>
      <p>
        App service <strong>{result?.name}</strong> created with {result?.componentCount} component mapping(s).
      </p>
      {result?.topology?.nodes?.length > 0 && (
        <div>
          <p className="form-label">Runtime topology</p>
          {result.topology.nodes.map((n) => (
            <div key={n.id} className="muted" style={{ fontSize: "0.82rem" }}>
              {n.componentName}: <code>{n.resolved || "—"}</code>
              <span className="status-badge status-badge--pending" style={{ marginLeft: "0.4rem" }}>{n.status}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );

  const canNext = step === 0 ? Boolean(target.clusterId) : true;

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}
        style={{ maxHeight: "90vh", overflowY: "auto" }}>
        <div className="modal-card__header">
          <h3>Deploy From Blueprint</h3>
          <p className="muted">{blueprintName}</p>
        </div>

        <StepHeader step={step} />
        {error && <p className="banner-message error">{error}</p>}

        {step === 0 && renderTarget()}
        {step === 1 && renderMapping()}
        {step === 2 && renderValues()}
        {step === 3 && renderReview()}
        {step === 4 && renderDone()}

        <div className="modal-actions">
          {step === 4 ? (
            <button type="button" className="primary" onClick={onClose}>Done</button>
          ) : (
            <>
              <button type="button" className="btn-outline" onClick={onClose} disabled={busy}>Cancel</button>
              {step > 0 && (
                <button type="button" className="btn-outline" onClick={() => setStep((s) => s - 1)} disabled={busy}>
                  Back
                </button>
              )}
              {step === 0 && (
                <button type="button" className="primary" onClick={goToMapping} disabled={busy || !canNext}>
                  {busy ? "Loading…" : "Next"}
                </button>
              )}
              {(step === 1 || step === 2) && (
                <button type="button" className="primary" onClick={() => setStep((s) => s + 1)} disabled={busy}>
                  Next
                </button>
              )}
              {step === 3 && (
                <button type="button" className="primary" onClick={deploy} disabled={busy}>
                  {busy ? "Deploying…" : "Deploy now"}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
