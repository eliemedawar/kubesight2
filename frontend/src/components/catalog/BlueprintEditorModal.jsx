import { useMemo, useState } from "react";

// Mirrors the backend VALID_* sets (service_blueprint_service.py).
const COMPONENT_TYPES = [
  "deployment", "statefulset", "daemonset", "cronjob", "service", "ingress",
  "database", "redis", "kafka", "worker", "external_service", "cache", "queue",
];
const REQUIREMENT_TYPES = [
  "env_var", "secret", "configmap", "pvc", "ingress_host", "tls_secret",
  "image_pull_secret", "hpa", "resource_limit", "database_credential",
  "external_endpoint",
];
const VALUE_SOURCES = [
  "manual", "dropdown", "existing_secret", "existing_configmap", "generated",
  "blueprint_default", "detected_from_cluster",
];
const CRITICALITIES = ["low", "medium", "high", "critical"];
const STATUSES = ["draft", "ready", "deprecated"];
const PROTOCOLS = [
  "HTTP", "HTTPS", "gRPC", "TCP", "UDP", "WebSocket", "AMQP", "MQTT",
  "Kafka", "Redis", "PostgreSQL", "MySQL", "MongoDB", "GraphQL",
];
const DEFAULT_CATEGORIES = [
  "Platform", "Payments", "Communication", "Data", "Security",
  "Infrastructure", "Analytics", "Integration", "Internal Tools",
];

const CREATE_OPTION = "__create__";

let tempCounter = 0;
const nextTempId = () => `t${Date.now()}_${tempCounter++}`;

function fromBlueprint(detail) {
  if (!detail) {
    return {
      name: "", description: "", category: "", ownerTeam: "",
      criticality: "medium", status: "draft", version: "1.0.0",
      components: [], connections: [], requirements: [],
    };
  }
  // Use existing ids as temp ids so connections/requirements keep resolving.
  return {
    name: detail.name || "",
    description: detail.description || "",
    category: detail.category || "",
    ownerTeam: detail.ownerTeam || "",
    criticality: detail.criticality || "medium",
    status: detail.status || "draft",
    version: detail.version || "1.0.0",
    components: (detail.components || []).map((c) => ({
      tempId: String(c.id),
      name: c.name,
      role: c.role || "",
      componentType: c.componentType || "deployment",
      required: c.required !== false,
      supportsExternal: Boolean(c.supportsExternal),
      defaultTemplateId: c.defaultTemplateId || "",
      defaultPort: c.defaultPort ?? "",
      description: c.description || "",
    })),
    connections: (detail.connections || []).map((cn) => ({
      tempId: nextTempId(),
      sourceTempId: String(cn.sourceComponentId),
      targetTempId: String(cn.targetComponentId),
      protocol: cn.protocol || "",
      port: cn.port ?? "",
    })),
    requirements: (detail.requirements || []).map((r) => ({
      tempId: nextTempId(),
      key: r.key,
      requirementType: r.requirementType || "env_var",
      required: r.required !== false,
      valueSource: r.valueSource || "manual",
      secret: Boolean(r.secret),
      autoGenerate: Boolean(r.autoGenerate),
      defaultValue: r.defaultValue || "",
      componentTempId: r.componentId ? String(r.componentId) : "",
    })),
  };
}

export default function BlueprintEditorModal({ blueprint, categories = [], onClose, onSave, saving, error }) {
  const isEdit = Boolean(blueprint?.id);
  const [form, setForm] = useState(() => fromBlueprint(blueprint));
  // Start in "create" mode if editing a blueprint whose category isn't a known one.
  const [creatingCategory, setCreatingCategory] = useState(false);

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const categoryOptions = useMemo(() => {
    const merged = [...DEFAULT_CATEGORIES, ...categories];
    if (form.category) merged.push(form.category);
    return [...new Set(merged.filter(Boolean))].sort((a, b) => a.localeCompare(b));
  }, [categories, form.category]);

  const componentOptions = useMemo(
    () => form.components.filter((c) => c.name.trim()),
    [form.components]
  );

  // --- component helpers ---------------------------------------------------
  const addComponent = () =>
    setForm((prev) => ({
      ...prev,
      components: [
        ...prev.components,
        {
          tempId: nextTempId(), name: "", role: "", componentType: "deployment",
          required: true, supportsExternal: false, defaultTemplateId: "",
          defaultPort: "", description: "",
        },
      ],
    }));

  const updateComponent = (tempId, key, value) =>
    setForm((prev) => ({
      ...prev,
      components: prev.components.map((c) => (c.tempId === tempId ? { ...c, [key]: value } : c)),
    }));

  const removeComponent = (tempId) =>
    setForm((prev) => ({
      ...prev,
      components: prev.components.filter((c) => c.tempId !== tempId),
      connections: prev.connections.filter(
        (cn) => cn.sourceTempId !== tempId && cn.targetTempId !== tempId
      ),
      requirements: prev.requirements.map((r) =>
        r.componentTempId === tempId ? { ...r, componentTempId: "" } : r
      ),
    }));

  // --- connection helpers --------------------------------------------------
  const addConnection = () =>
    setForm((prev) => ({
      ...prev,
      connections: [
        ...prev.connections,
        { tempId: nextTempId(), sourceTempId: "", targetTempId: "", protocol: "", port: "" },
      ],
    }));

  const updateConnection = (tempId, key, value) =>
    setForm((prev) => ({
      ...prev,
      connections: prev.connections.map((cn) => (cn.tempId === tempId ? { ...cn, [key]: value } : cn)),
    }));

  const removeConnection = (tempId) =>
    setForm((prev) => ({
      ...prev,
      connections: prev.connections.filter((cn) => cn.tempId !== tempId),
    }));

  // --- requirement helpers -------------------------------------------------
  const addRequirement = () =>
    setForm((prev) => ({
      ...prev,
      requirements: [
        ...prev.requirements,
        {
          tempId: nextTempId(), key: "", requirementType: "env_var", required: true,
          valueSource: "manual", secret: false, autoGenerate: false,
          defaultValue: "", componentTempId: "",
        },
      ],
    }));

  const updateRequirement = (tempId, key, value) =>
    setForm((prev) => ({
      ...prev,
      requirements: prev.requirements.map((r) => (r.tempId === tempId ? { ...r, [key]: value } : r)),
    }));

  const removeRequirement = (tempId) =>
    setForm((prev) => ({
      ...prev,
      requirements: prev.requirements.filter((r) => r.tempId !== tempId),
    }));

  const handleSubmit = () => {
    if (!form.name.trim()) return;
    onSave({
      name: form.name.trim(),
      description: form.description.trim() || undefined,
      category: form.category.trim() || undefined,
      ownerTeam: form.ownerTeam.trim() || undefined,
      criticality: form.criticality || undefined,
      status: form.status,
      version: form.version.trim() || "1.0.0",
      components: form.components
        .filter((c) => c.name.trim())
        .map((c, index) => ({
          tempId: c.tempId,
          name: c.name.trim(),
          role: c.role.trim() || undefined,
          componentType: c.componentType,
          required: c.required,
          supportsExternal: c.supportsExternal,
          defaultTemplateId: c.defaultTemplateId.trim() || undefined,
          defaultPort: c.defaultPort === "" ? undefined : Number(c.defaultPort),
          description: c.description.trim() || undefined,
          position: index,
        })),
      connections: form.connections
        .filter((cn) => cn.sourceTempId && cn.targetTempId && cn.sourceTempId !== cn.targetTempId)
        .map((cn) => ({
          sourceTempId: cn.sourceTempId,
          targetTempId: cn.targetTempId,
          protocol: cn.protocol.trim() || undefined,
          port: cn.port === "" ? undefined : Number(cn.port),
        })),
      requirements: form.requirements
        .filter((r) => r.key.trim())
        .map((r) => ({
          key: r.key.trim(),
          requirementType: r.requirementType,
          required: r.required,
          valueSource: r.valueSource,
          secret: r.secret,
          autoGenerate: r.autoGenerate,
          defaultValue: r.defaultValue.trim() || undefined,
          componentTempId: r.componentTempId || undefined,
        })),
    });
  };

  const componentLabel = (tempId) =>
    componentOptions.find((c) => c.tempId === tempId)?.name || "—";

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card modal-card--wide"
        role="dialog"
        onClick={(e) => e.stopPropagation()}
        style={{ maxHeight: "90vh", overflowY: "auto" }}
      >
        <div className="modal-card__header">
          <h3>{isEdit ? "Edit Service Blueprint" : "New Service Blueprint"}</h3>
          <p className="muted">
            Define the logical service design — components, topology, and requirements.
            Real Kubernetes resources are chosen later at Deploy From Blueprint time.
          </p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        {/* Basic info */}
        <section className="form-section">
          <h4>Basic information</h4>
          <div className="form-grid">
            <label className="form-grid__full">
              Name *
              <input value={form.name} maxLength={120} placeholder="e.g. QR Code Service"
                onChange={(e) => set("name", e.target.value)} />
            </label>
            <label className="form-grid__full">
              Description
              <textarea value={form.description} rows={2} style={{ resize: "vertical" }}
                onChange={(e) => set("description", e.target.value)} />
            </label>
            <label>
              Category
              {creatingCategory ? (
                <input value={form.category} maxLength={80} placeholder="New category"
                  autoFocus onChange={(e) => set("category", e.target.value)} />
              ) : (
                <select
                  value={categoryOptions.includes(form.category) ? form.category : ""}
                  onChange={(e) => {
                    if (e.target.value === CREATE_OPTION) {
                      setCreatingCategory(true);
                      set("category", "");
                    } else {
                      set("category", e.target.value);
                    }
                  }}
                >
                  <option value="">— Select —</option>
                  {categoryOptions.map((c) => <option key={c} value={c}>{c}</option>)}
                  <option value={CREATE_OPTION}>+ Create new category…</option>
                </select>
              )}
              {creatingCategory && (
                <button type="button" className="link-button"
                  onClick={() => { setCreatingCategory(false); set("category", ""); }}
                  style={{ background: "none", border: "none", padding: 0, cursor: "pointer",
                    color: "var(--accent, #2563eb)", fontSize: "0.75rem", marginTop: "0.2rem", textAlign: "left" }}>
                  Choose existing instead
                </button>
              )}
            </label>
            <label>
              Owner team
              <input value={form.ownerTeam} maxLength={255} placeholder="e.g. Payments"
                onChange={(e) => set("ownerTeam", e.target.value)} />
            </label>
            <label>
              Criticality
              <select value={form.criticality} onChange={(e) => set("criticality", e.target.value)}>
                {CRITICALITIES.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            <label>
              Status
              <select value={form.status} onChange={(e) => set("status", e.target.value)}>
                {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label>
              Version
              <input value={form.version} maxLength={32} placeholder="1.0.0"
                onChange={(e) => set("version", e.target.value)} />
            </label>
          </div>
        </section>

        {/* Components */}
        <section className="form-section">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h4>Components</h4>
            <button type="button" className="btn-outline btn-compact" onClick={addComponent}>+ Add component</button>
          </div>
          {form.components.length === 0 ? (
            <p className="muted">No components yet. Add logical components like Frontend, Backend API, Database.</p>
          ) : (
            form.components.map((c) => (
              <div key={c.tempId} className="card" style={{ padding: "0.85rem", marginTop: "0.6rem" }}>
                <div className="form-grid">
                  <label>
                    Name *
                    <input value={c.name} maxLength={120} placeholder="Backend API"
                      onChange={(e) => updateComponent(c.tempId, "name", e.target.value)} />
                  </label>
                  <label>
                    Role
                    <input value={c.role} maxLength={120} placeholder="backend"
                      onChange={(e) => updateComponent(c.tempId, "role", e.target.value)} />
                  </label>
                  <label>
                    Type
                    <select value={c.componentType} onChange={(e) => updateComponent(c.tempId, "componentType", e.target.value)}>
                      {COMPONENT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </label>
                  <label>
                    Default template (slug)
                    <input value={c.defaultTemplateId} maxLength={120} placeholder="flask-backend"
                      onChange={(e) => updateComponent(c.tempId, "defaultTemplateId", e.target.value)} />
                  </label>
                  <label>
                    Default port
                    <input type="number" value={c.defaultPort} placeholder="8000"
                      onChange={(e) => updateComponent(c.tempId, "defaultPort", e.target.value)} />
                  </label>
                  <label className="checkbox-row">
                    <input type="checkbox" checked={c.required}
                      onChange={(e) => updateComponent(c.tempId, "required", e.target.checked)} />
                    Required
                  </label>
                  <label className="checkbox-row">
                    <input type="checkbox" checked={c.supportsExternal}
                      onChange={(e) => updateComponent(c.tempId, "supportsExternal", e.target.checked)} />
                    Can be external
                  </label>
                </div>
                <div style={{ textAlign: "right", marginTop: "0.4rem" }}>
                  <button type="button" className="btn-outline btn-compact danger"
                    onClick={() => removeComponent(c.tempId)}>Remove</button>
                </div>
              </div>
            ))
          )}
        </section>

        {/* Topology / connections */}
        <section className="form-section">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h4>Topology connections</h4>
            <button type="button" className="btn-outline btn-compact" onClick={addConnection}
              disabled={componentOptions.length < 2}>+ Add connection</button>
          </div>
          {componentOptions.length < 2 ? (
            <p className="muted">Add at least two components to connect them.</p>
          ) : form.connections.length === 0 ? (
            <p className="muted">No connections. Example: Frontend → Backend API → Database.</p>
          ) : (
            form.connections.map((cn) => (
              <div key={cn.tempId} style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginTop: "0.5rem", flexWrap: "wrap" }}>
                <select value={cn.sourceTempId} onChange={(e) => updateConnection(cn.tempId, "sourceTempId", e.target.value)}>
                  <option value="">From…</option>
                  {componentOptions.map((c) => <option key={c.tempId} value={c.tempId}>{c.name}</option>)}
                </select>
                <span aria-hidden="true">→</span>
                <select value={cn.targetTempId} onChange={(e) => updateConnection(cn.tempId, "targetTempId", e.target.value)}>
                  <option value="">To…</option>
                  {componentOptions.map((c) => <option key={c.tempId} value={c.tempId}>{c.name}</option>)}
                </select>
                <select value={cn.protocol} style={{ maxWidth: 120 }}
                  onChange={(e) => updateConnection(cn.tempId, "protocol", e.target.value)}>
                  <option value="">Protocol…</option>
                  {PROTOCOLS.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
                <input type="number" value={cn.port} placeholder="port" style={{ maxWidth: 90 }}
                  onChange={(e) => updateConnection(cn.tempId, "port", e.target.value)} />
                <button type="button" className="btn-outline btn-compact danger"
                  onClick={() => removeConnection(cn.tempId)}>×</button>
              </div>
            ))
          )}
        </section>

        {/* Requirements */}
        <section className="form-section">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h4>Requirements</h4>
            <button type="button" className="btn-outline btn-compact" onClick={addRequirement}>+ Add requirement</button>
          </div>
          {form.requirements.length === 0 ? (
            <p className="muted">No requirements. Add env vars, secrets, ingress host, TLS secret, HPA, etc.</p>
          ) : (
            form.requirements.map((r) => (
              <div key={r.tempId} className="card" style={{ padding: "0.85rem", marginTop: "0.6rem" }}>
                <div className="form-grid">
                  <label>
                    Key *
                    <input value={r.key} maxLength={120} placeholder="INGRESS_HOST"
                      onChange={(e) => updateRequirement(r.tempId, "key", e.target.value)} />
                  </label>
                  <label>
                    Type
                    <select value={r.requirementType} onChange={(e) => updateRequirement(r.tempId, "requirementType", e.target.value)}>
                      {REQUIREMENT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </label>
                  <label>
                    Value source
                    <select value={r.valueSource} onChange={(e) => updateRequirement(r.tempId, "valueSource", e.target.value)}>
                      {VALUE_SOURCES.map((v) => <option key={v} value={v}>{v}</option>)}
                    </select>
                  </label>
                  <label>
                    Scope (component)
                    <select value={r.componentTempId} onChange={(e) => updateRequirement(r.tempId, "componentTempId", e.target.value)}>
                      <option value="">Whole blueprint</option>
                      {componentOptions.map((c) => <option key={c.tempId} value={c.tempId}>{c.name}</option>)}
                    </select>
                  </label>
                  <label>
                    Default value
                    <input value={r.defaultValue} placeholder="optional"
                      onChange={(e) => updateRequirement(r.tempId, "defaultValue", e.target.value)} />
                  </label>
                  <label className="checkbox-row">
                    <input type="checkbox" checked={r.required}
                      onChange={(e) => updateRequirement(r.tempId, "required", e.target.checked)} />
                    Required
                  </label>
                  <label className="checkbox-row">
                    <input type="checkbox" checked={r.secret}
                      onChange={(e) => updateRequirement(r.tempId, "secret", e.target.checked)} />
                    Secret
                  </label>
                  <label className="checkbox-row">
                    <input type="checkbox" checked={r.autoGenerate}
                      onChange={(e) => updateRequirement(r.tempId, "autoGenerate", e.target.checked)} />
                    Auto-generate
                  </label>
                </div>
                <div style={{ textAlign: "right", marginTop: "0.4rem" }}>
                  <button type="button" className="btn-outline btn-compact danger"
                    onClick={() => removeRequirement(r.tempId)}>Remove</button>
                </div>
              </div>
            ))
          )}
        </section>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>Cancel</button>
          <button type="button" className="primary" onClick={handleSubmit} disabled={saving || !form.name.trim()}>
            {saving ? "Saving…" : isEdit ? "Save changes" : "Create blueprint"}
          </button>
        </div>
      </div>
    </div>
  );
}
