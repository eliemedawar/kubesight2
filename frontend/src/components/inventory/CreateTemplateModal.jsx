import { useEffect, useMemo, useState } from "react";

const WORKLOAD_TYPES = ["Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"];
const PULL_POLICIES = ["IfNotPresent", "Always", "Never"];
const NEW_CATEGORY = "__new__";

// The author picks a single *kind* per variable. The deployer later chooses
// "existing vs create" for ConfigMap/Secret kinds. Sensitive forces Secret.
const ENV_KINDS = [
  { value: "value", label: "Plain value" },
  { value: "configMap", label: "ConfigMap" },
  { value: "secret", label: "Secret" },
];
const KIND_SOURCES = {
  value: ["value"],
  configMap: ["existingConfigMap", "createConfigMap"],
  secret: ["existingSecret", "createSecret"],
};

// A volume mount sources a whole ConfigMap or Secret as files on disk. The author
// picks the kind; the deployer later chooses existing-vs-create at deploy time.
const VOLUME_KINDS = [
  { value: "configMap", label: "ConfigMap" },
  { value: "secret", label: "Secret" },
];
const VOLUME_KIND_SOURCES = {
  configMap: ["existingConfigMap", "createConfigMap"],
  secret: ["existingSecret", "createSecret"],
};

const DEPENDENCY_KINDS = ["postgresql", "redis", "kafka", "rabbitmq", "minio", "service"];
const SERVICE_TYPES = ["ClusterIP", "NodePort", "LoadBalancer"];
const PROBE_TYPES = [
  { value: "http", label: "HTTP GET" },
  { value: "tcp", label: "TCP socket" },
  { value: "command", label: "Command" },
];
const PROBE_KEYS = [
  { key: "readiness", label: "Readiness probe" },
  { key: "liveness", label: "Liveness probe" },
  { key: "startup", label: "Startup probe" },
];

// A row in the environment-variable *schema*: requirements a deployer fills later,
// not a concrete value. `default` pre-fills the deployer's field for plain-value vars.
const EMPTY_ENV = { key: "", required: false, sensitive: false, kind: "value", default: "" };

// Dependencies are informational only — they document the backing services an
// app needs; they don't wire anything automatically.
const EMPTY_DEPENDENCY = { kind: "postgresql", name: "", required: true, note: "" };

// A volume-mount schema row: the deployer supplies the actual ConfigMap/Secret
// (existing or freshly created) at deploy time.
const EMPTY_VOLUME = { mountPath: "", kind: "configMap", readOnly: false };

// Every override the deployer may be granted lives here — the single home for the
// lock-vs-override decision.
const DEFAULT_OVERRIDES = {
  image: false,
  tag: true,
  replicas: true,
  resources: false,
  storageSize: false,
  ingressHost: false,
  serviceTypes: [],
};

const PROBE_DEFAULTS = {
  readiness: { enabled: false, type: "http", path: "/", port: "", command: "", initialDelaySeconds: "5", periodSeconds: "10" },
  liveness: { enabled: false, type: "http", path: "/", port: "", command: "", initialDelaySeconds: "15", periodSeconds: "20" },
  startup: { enabled: false, type: "http", path: "/", port: "", command: "", initialDelaySeconds: "10", periodSeconds: "10" },
};

function freshProbes() {
  return {
    readiness: { ...PROBE_DEFAULTS.readiness },
    liveness: { ...PROBE_DEFAULTS.liveness },
    startup: { ...PROBE_DEFAULTS.startup },
  };
}

const EMPTY_FORM = {
  name: "",
  description: "",
  category: "",
  newCategory: "",
  workloadType: "Deployment",
  // Container
  image: "",
  tag: "latest",
  port: "80",
  replicas: "1",
  pullPolicy: "IfNotPresent",
  serviceEnabled: true,
  // Resources
  cpuRequest: "100m",
  cpuLimit: "500m",
  memoryRequest: "128Mi",
  memoryLimit: "256Mi",
  // Deployment schema
  envVars: [{ ...EMPTY_ENV }],
  overrides: { ...DEFAULT_OVERRIDES },
  dependencies: [],
  volumeMounts: [],
  ingressTls: false,
  // Storage (simplified — advanced PVC/PV details are no longer authored here)
  storageEnabled: false,
  storageSize: "1Gi",
  storageMountPath: "/data",
  probes: freshProbes(),
};

function freshForm(category) {
  return {
    ...EMPTY_FORM,
    category,
    envVars: [{ ...EMPTY_ENV }],
    overrides: { ...DEFAULT_OVERRIDES, serviceTypes: [] },
    dependencies: [],
    volumeMounts: [],
    probes: freshProbes(),
  };
}

// Rebuild the editable form from a saved template detail — the inverse of the
// payload `handleSubmit` assembles, so an existing template round-trips cleanly.
function formFromTemplate(detail) {
  const container = detail.containers?.[0] || {};
  const res = detail.resources || {};
  const schema = detail.schema || {};
  const overrides = schema.overrides || {};
  const storage = detail.storage || {};
  const service = detail.networking?.service;

  const envVars = (schema.env || []).map((f) => ({
    key: f.key || "",
    required: Boolean(f.required),
    sensitive: Boolean(f.sensitive),
    kind: f.sensitive ? "secret" : f.kind || "value",
    default: f.default || "",
  }));
  const dependencies = (schema.dependencies || []).map((d) => ({
    kind: d.kind || "postgresql",
    name: d.name || "",
    required: Boolean(d.required),
    note: d.note || "",
  }));
  const volumeMounts = (schema.volumeMounts || []).map((v) => ({
    mountPath: v.mountPath || "",
    kind: v.kind === "secret" ? "secret" : "configMap",
    readOnly: Boolean(v.readOnly),
  }));

  const probes = freshProbes();
  for (const { key } of PROBE_KEYS) {
    const hc = detail.healthChecks?.[key];
    if (!hc) continue;
    probes[key] = {
      enabled: true,
      type: hc.type || "http",
      path: hc.path || "/",
      port: hc.port != null ? String(hc.port) : "",
      command: hc.command || "",
      initialDelaySeconds: String(hc.initialDelaySeconds ?? PROBE_DEFAULTS[key].initialDelaySeconds),
      periodSeconds: String(hc.periodSeconds ?? PROBE_DEFAULTS[key].periodSeconds),
    };
  }

  return {
    ...EMPTY_FORM,
    name: detail.name || "",
    description: detail.description || "",
    category: detail.category || "Custom",
    newCategory: "",
    workloadType: detail.workloadType || "Deployment",
    image: container.image || "",
    tag: container.tag || "latest",
    port: container.ports?.[0] != null ? String(container.ports[0]) : "80",
    replicas: detail.scaling?.replicas != null ? String(detail.scaling.replicas) : "1",
    pullPolicy: container.pullPolicy || "IfNotPresent",
    serviceEnabled: service ? Boolean(service.enabled) : false,
    cpuRequest: res.cpuRequest || "100m",
    cpuLimit: res.cpuLimit || "500m",
    memoryRequest: res.memoryRequest || "128Mi",
    memoryLimit: res.memoryLimit || "256Mi",
    envVars: envVars.length ? envVars : [{ ...EMPTY_ENV }],
    overrides: {
      image: Boolean(overrides.image),
      tag: Boolean(overrides.tag),
      replicas: Boolean(overrides.replicas),
      resources: Boolean(overrides.resources),
      storageSize: Boolean(overrides.storageSize),
      ingressHost: Boolean(schema.ingress?.supported),
      serviceTypes: Array.isArray(overrides.serviceType) ? overrides.serviceType : [],
    },
    dependencies,
    volumeMounts,
    ingressTls: Boolean(schema.ingress?.tls),
    storageEnabled: Boolean(storage.pvcMode && storage.pvcMode !== "none"),
    storageSize: storage.newPvc?.size || "1Gi",
    storageMountPath: storage.volumeMounts?.[0]?.mountPath || "/data",
    probes,
  };
}

export default function CreateTemplateModal({ open, existingCategories = [], template = null, onClose, onSubmit }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  // Which button is mid-flight, so we can label just that one "Saving…".
  const [pendingAction, setPendingAction] = useState("");
  const isEditing = Boolean(template);

  const categoryOptions = useMemo(() => {
    const seen = new Set();
    const ordered = [];
    for (const category of existingCategories) {
      const trimmed = (category || "").trim();
      if (trimmed && !seen.has(trimmed)) {
        seen.add(trimmed);
        ordered.push(trimmed);
      }
    }
    return ordered;
  }, [existingCategories]);

  useEffect(() => {
    if (open) {
      setForm(template ? formFromTemplate(template) : freshForm(categoryOptions[0] || NEW_CATEGORY));
      setError("");
      setSaving(false);
      setPendingAction("");
    }
  }, [open, template, categoryOptions]);

  // --- env schema handlers ---
  const setEnvKind = (index, kind) =>
    setForm((prev) => ({
      ...prev,
      envVars: prev.envVars.map((row, i) => {
        if (i !== index) return row;
        // Only plain-value vars carry a default.
        return { ...row, kind, default: kind === "value" ? row.default : "" };
      }),
    }));

  // Sensitive variables must flow through a Secret, so flipping it forces the
  // Secret kind and clears any (now unsafe) default.
  const toggleEnvSensitive = (index) =>
    setForm((prev) => ({
      ...prev,
      envVars: prev.envVars.map((row, i) => {
        if (i !== index) return row;
        const sensitive = !row.sensitive;
        if (!sensitive) return { ...row, sensitive };
        return { ...row, sensitive, kind: "secret", default: "" };
      }),
    }));

  // --- override handlers ---
  const toggleOverride = (key) => (event) =>
    setForm((prev) => ({ ...prev, overrides: { ...prev.overrides, [key]: event.target.checked } }));

  const toggleServiceType = (type) =>
    setForm((prev) => {
      const current = prev.overrides.serviceTypes || [];
      const next = current.includes(type) ? current.filter((t) => t !== type) : [...current, type];
      return { ...prev, overrides: { ...prev.overrides, serviceTypes: next } };
    });

  // --- dependency handlers ---
  const updateDependency = (index, key) => (event) => {
    const value = event.target.type === "checkbox" ? event.target.checked : event.target.value;
    setForm((prev) => ({
      ...prev,
      dependencies: prev.dependencies.map((dep, i) => (i === index ? { ...dep, [key]: value } : dep)),
    }));
  };
  const addDependency = () =>
    setForm((prev) => ({ ...prev, dependencies: [...prev.dependencies, { ...EMPTY_DEPENDENCY }] }));
  const removeDependency = (index) =>
    setForm((prev) => ({ ...prev, dependencies: prev.dependencies.filter((_, i) => i !== index) }));

  if (!open) return null;

  const update = (key) => (event) => setForm((prev) => ({ ...prev, [key]: event.target.value }));
  const toggle = (key) => (event) => setForm((prev) => ({ ...prev, [key]: event.target.checked }));

  // Generic handlers for repeatable list fields (envVars).
  const updateRow = (field, index, key) => (event) => {
    const value = event.target.type === "checkbox" ? event.target.checked : event.target.value;
    setForm((prev) => ({
      ...prev,
      [field]: prev[field].map((row, i) => (i === index ? { ...row, [key]: value } : row)),
    }));
  };
  const addRow = (field, empty) => () =>
    setForm((prev) => ({ ...prev, [field]: [...prev[field], { ...empty }] }));
  const removeRow = (field, empty, index) => () =>
    setForm((prev) => {
      const next = prev[field].filter((_, i) => i !== index);
      return { ...prev, [field]: next.length ? next : [{ ...empty }] };
    });

  const updateProbe = (probeKey, field) => (event) => {
    const value = event.target.type === "checkbox" ? event.target.checked : event.target.value;
    setForm((prev) => ({
      ...prev,
      probes: { ...prev.probes, [probeKey]: { ...prev.probes[probeKey], [field]: value } },
    }));
  };

  const resolveCategory = () => (form.category === NEW_CATEGORY ? form.newCategory : form.category).trim();

  const buildProbe = (probe, fallbackPort) => {
    if (!probe.enabled) return null;
    const initialDelaySeconds = Number.parseInt(probe.initialDelaySeconds, 10);
    const periodSeconds = Number.parseInt(probe.periodSeconds, 10);
    const port = Number.parseInt(probe.port, 10);
    const built = {
      enabled: true,
      type: probe.type,
      initialDelaySeconds: Number.isFinite(initialDelaySeconds) ? initialDelaySeconds : 5,
      periodSeconds: Number.isFinite(periodSeconds) ? periodSeconds : 10,
    };
    if (probe.type === "command") {
      built.command = probe.command.trim();
    } else {
      built.port = Number.isFinite(port) ? port : fallbackPort;
      if (probe.type === "http") built.path = probe.path.trim() || "/";
    }
    return built;
  };

  const slug = (value) => value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

  // `asNewVersion` forks the edited template into a fresh "vN" copy instead of
  // overwriting the original — the parent decides the new name and calls create.
  const handleSubmit = async (asNewVersion = false) => {
    setError("");
    const name = form.name.trim();
    if (!name) {
      setError("Template name is required.");
      return;
    }
    const category = resolveCategory();
    if (!category) {
      setError("Choose an existing category or enter a new one.");
      return;
    }
    if (!form.image.trim()) {
      setError("Container image is required.");
      return;
    }

    const port = Number.parseInt(form.port, 10);
    const replicas = Number.parseInt(form.replicas, 10);
    const containerName = slug(name) || "main";
    const payload = {
      name,
      description: form.description.trim(),
      category,
      workloadType: form.workloadType,
      containers: [
        {
          name: containerName,
          image: form.image.trim(),
          tag: form.tag.trim() || "latest",
          pullPolicy: form.pullPolicy,
          ports: Number.isFinite(port) ? [port] : [],
        },
      ],
      resources: {
        cpuRequest: form.cpuRequest.trim(),
        cpuLimit: form.cpuLimit.trim(),
        memoryRequest: form.memoryRequest.trim(),
        memoryLimit: form.memoryLimit.trim(),
      },
      scaling: { replicas: Number.isFinite(replicas) ? replicas : 1 },
    };
    if (form.serviceEnabled && Number.isFinite(port)) {
      payload.networking = {
        service: { enabled: true, type: "ClusterIP", port, targetPort: port, protocol: "TCP" },
      };
    }

    // Environment-variable *schema*: each row declares a kind + requirements, not
    // a value. The deployer chooses existing-vs-create for ConfigMap/Secret kinds.
    const envSchema = form.envVars
      .map((row) => {
        const key = (row.key || "").trim();
        if (!key) return null;
        const sensitive = Boolean(row.sensitive);
        const kind = sensitive ? "secret" : row.kind || "value";
        const field = { key, required: Boolean(row.required), sensitive, kind, allowedSources: KIND_SOURCES[kind] };
        if (kind === "value" && row.default.trim()) field.default = row.default.trim();
        return field;
      })
      .filter(Boolean);

    // Carry non-sensitive defaults through as legacy prefills so summaries stay populated.
    const envVars = envSchema.filter((f) => f.default).map((f) => ({ name: f.key, value: f.default }));
    if (envVars.length) payload.environment = { envVars };

    // Storage (simplified): a single PVC + mount, no advanced PV authoring.
    if (form.storageEnabled) {
      payload.storage = {
        pvcMode: "new",
        newPvc: { name: `${containerName}-data`, size: form.storageSize.trim() || "1Gi", accessMode: "ReadWriteOnce" },
        volumeMounts: [{ name: "data", mountPath: form.storageMountPath.trim() || "/data", readOnly: false }],
      };
    }

    // Health checks.
    const fallbackPort = Number.isFinite(port) ? port : 80;
    const healthChecks = {};
    for (const { key } of PROBE_KEYS) {
      const built = buildProbe(form.probes[key], fallbackPort);
      if (built) healthChecks[key] = built;
    }
    if (Object.keys(healthChecks).length) payload.healthChecks = healthChecks;

    // Deployment schema: overrides, env requirements, dependencies, ingress, pull secret.
    const schema = {};
    const overrides = {};
    for (const key of ["image", "tag", "replicas", "resources", "storageSize"]) {
      if (form.overrides[key]) overrides[key] = true;
    }
    if ((form.overrides.serviceTypes || []).length) overrides.serviceType = form.overrides.serviceTypes;
    if (Object.keys(overrides).length) schema.overrides = overrides;
    if (envSchema.length) schema.env = envSchema;

    const dependencies = form.dependencies
      .map((dep) => ({
        kind: dep.kind,
        name: (dep.name || dep.kind).trim(),
        required: Boolean(dep.required),
        ...(dep.note?.trim() ? { note: dep.note.trim() } : {}),
      }))
      .filter((dep) => dep.name);
    if (dependencies.length) schema.dependencies = dependencies;

    // Volume mounts: each declares a ConfigMap/Secret-backed mount path. The
    // deployer chooses existing-vs-create for the backing resource at deploy time.
    const volumeMounts = form.volumeMounts
      .map((row) => {
        const mountPath = (row.mountPath || "").trim();
        if (!mountPath) return null;
        const kind = row.kind === "secret" ? "secret" : "configMap";
        return { mountPath, kind, allowedSources: VOLUME_KIND_SOURCES[kind], readOnly: Boolean(row.readOnly) };
      })
      .filter(Boolean);
    if (volumeMounts.length) schema.volumeMounts = volumeMounts;

    if (form.overrides.ingressHost) {
      schema.ingress = { supported: true, tls: Boolean(form.ingressTls) };
    }

    if (Object.keys(schema).length) payload.schema = schema;

    setSaving(true);
    setPendingAction(asNewVersion ? "version" : "save");
    try {
      await onSubmit(payload, { asNewVersion });
    } catch (err) {
      const fallback = asNewVersion
        ? "Failed to create new version."
        : isEditing
          ? "Failed to update template."
          : "Failed to create template.";
      setError(err.message || fallback);
      setSaving(false);
      setPendingAction("");
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>{isEditing ? "Edit Template" : "Create Template"}</h3>
          <p className="muted">
            Define a reusable <strong>schema</strong> — what a deployment requires, what it may override, and what stays
            locked. Actual values are supplied at deploy time, never stored here.
          </p>
        </div>

        {error ? <p className="banner-message error">{error}</p> : null}

        <section className="form-section">
          <h4>Details</h4>
          <div className="form-grid">
            <label>
              Template name
              <input value={form.name} onChange={update("name")} placeholder="e.g. Internal API" />
            </label>
            <label>
              Workload type
              <select value={form.workloadType} onChange={update("workloadType")}>
                {WORKLOAD_TYPES.map((type) => (
                  <option key={type} value={type}>{type}</option>
                ))}
              </select>
            </label>
            <label className="form-grid__full">
              Description
              <input value={form.description} onChange={update("description")} placeholder="What this template deploys" />
            </label>
            <label>
              Category
              <select value={form.category} onChange={update("category")}>
                {categoryOptions.map((category) => (
                  <option key={category} value={category}>{category}</option>
                ))}
                <option value={NEW_CATEGORY}>+ New category…</option>
              </select>
            </label>
            {form.category === NEW_CATEGORY ? (
              <label>
                New category name
                <input value={form.newCategory} onChange={update("newCategory")} placeholder="e.g. Internal Tools" />
              </label>
            ) : null}
          </div>
        </section>

        <section className="form-section">
          <h4>Container</h4>
          <div className="form-grid">
            <label>
              Image
              <input value={form.image} onChange={update("image")} placeholder="e.g. nginx" />
            </label>
            <label>
              Tag
              <input value={form.tag} onChange={update("tag")} placeholder="latest" />
            </label>
            <label>
              Container port
              <input value={form.port} onChange={update("port")} inputMode="numeric" placeholder="80" />
            </label>
            <label>
              Replicas
              <input value={form.replicas} onChange={update("replicas")} inputMode="numeric" placeholder="1" />
            </label>
            <label>
              Image pull policy
              <select value={form.pullPolicy} onChange={update("pullPolicy")}>
                {PULL_POLICIES.map((policy) => (
                  <option key={policy} value={policy}>{policy}</option>
                ))}
              </select>
            </label>
            <label className="checkbox-row form-grid__full">
              <input type="checkbox" checked={form.serviceEnabled} onChange={toggle("serviceEnabled")} />
              <span>Expose a ClusterIP service on the container port</span>
            </label>
          </div>
          <p className="muted" style={{ marginTop: "var(--space-2)" }}>
            Image pull secrets are chosen by the deployer in the deployment wizard.
          </p>
        </section>

        <section className="form-section">
          <h4>Environment variable schema</h4>
          <p className="muted" style={{ marginTop: 0 }}>
            Pick each variable&apos;s source kind — Plain value, ConfigMap, or Secret. The deployer later chooses
            existing-vs-create for ConfigMap/Secret kinds. Marking a variable sensitive forces it through a Secret.
          </p>
          <div className="schema-env-list">
            {form.envVars.map((row, index) => (
              <div key={index} className="schema-env-card">
                <div className="schema-env-card__top">
                  <input
                    value={row.key}
                    onChange={updateRow("envVars", index, "key")}
                    placeholder="VAR_NAME"
                    aria-label={`Variable ${index + 1} key`}
                    className="schema-env-card__key"
                  />
                  <label className="checkbox-row">
                    <input type="checkbox" checked={row.required} onChange={updateRow("envVars", index, "required")} />
                    <span>Required</span>
                  </label>
                  <label className="checkbox-row">
                    <input type="checkbox" checked={row.sensitive} onChange={() => toggleEnvSensitive(index)} />
                    <span>Sensitive</span>
                  </label>
                  <button
                    type="button"
                    className="btn-outline template-env-row__remove"
                    onClick={removeRow("envVars", EMPTY_ENV, index)}
                    aria-label={`Remove variable ${index + 1}`}
                  >
                    ×
                  </button>
                </div>
                <div className="schema-env-card__sources" role="radiogroup" aria-label={`Variable ${index + 1} kind`}>
                  {ENV_KINDS.map((opt) => {
                    const disabled = row.sensitive && opt.value !== "secret";
                    const checked = (row.sensitive ? "secret" : row.kind) === opt.value;
                    return (
                      <label key={opt.value} className={`schema-source-chip${disabled ? " is-disabled" : ""}`}>
                        <input
                          type="radio"
                          name={`env-kind-${index}`}
                          checked={checked}
                          disabled={disabled}
                          onChange={() => setEnvKind(index, opt.value)}
                        />
                        <span>{opt.label}</span>
                      </label>
                    );
                  })}
                </div>
                {!row.sensitive && row.kind === "value" ? (
                  <input
                    value={row.default}
                    onChange={updateRow("envVars", index, "default")}
                    placeholder="Default value (optional)"
                    aria-label={`Variable ${index + 1} default`}
                    className="schema-env-card__default"
                  />
                ) : null}
              </div>
            ))}
          </div>
          <button type="button" className="btn-outline" onClick={addRow("envVars", EMPTY_ENV)}>
            + Add variable
          </button>
        </section>

        <section className="form-section">
          <h4>Dependencies</h4>
          <p className="muted" style={{ marginTop: 0 }}>
            Document the backing services this app needs. These are shown to the deployer for reference only.
          </p>
          {form.dependencies.length ? (
            <div className="schema-dep-list">
              {form.dependencies.map((dep, depIndex) => (
                <div key={depIndex} className="schema-dep-card">
                  <div className="schema-dep-card__top">
                    <select value={dep.kind} onChange={updateDependency(depIndex, "kind")} aria-label={`Dependency ${depIndex + 1} kind`}>
                      {DEPENDENCY_KINDS.map((kind) => (
                        <option key={kind} value={kind}>{kind}</option>
                      ))}
                    </select>
                    <input
                      value={dep.name}
                      onChange={updateDependency(depIndex, "name")}
                      placeholder="name (e.g. primary-db)"
                      aria-label={`Dependency ${depIndex + 1} name`}
                    />
                    <label className="checkbox-row">
                      <input type="checkbox" checked={dep.required} onChange={updateDependency(depIndex, "required")} />
                      <span>Required</span>
                    </label>
                    <button
                      type="button"
                      className="btn-outline template-env-row__remove"
                      onClick={() => removeDependency(depIndex)}
                      aria-label={`Remove dependency ${depIndex + 1}`}
                    >
                      ×
                    </button>
                  </div>
                  <input
                    value={dep.note || ""}
                    onChange={updateDependency(depIndex, "note")}
                    placeholder="Note for the deployer (optional)"
                    aria-label={`Dependency ${depIndex + 1} note`}
                  />
                </div>
              ))}
            </div>
          ) : null}
          <button type="button" className="btn-outline" onClick={addDependency}>
            + Add dependency
          </button>
        </section>

        <section className="form-section">
          <h4>Storage</h4>
          <label className="checkbox-row">
            <input type="checkbox" checked={form.storageEnabled} onChange={toggle("storageEnabled")} />
            <span>Enable persistent storage</span>
          </label>
          {form.storageEnabled ? (
            <div className="form-grid" style={{ marginTop: "var(--space-3)" }}>
              <label>
                Default size
                <input value={form.storageSize} onChange={update("storageSize")} placeholder="1Gi" />
              </label>
              <label>
                Mount path
                <input value={form.storageMountPath} onChange={update("storageMountPath")} placeholder="/data" />
              </label>
              <label className="checkbox-row form-grid__full">
                <input type="checkbox" checked={form.overrides.storageSize} onChange={toggleOverride("storageSize")} />
                <span>Allow the deployer to override the size</span>
              </label>
            </div>
          ) : null}
        </section>

        <section className="form-section">
          <h4>Volume mounts</h4>
          <p className="muted" style={{ marginTop: 0 }}>
            Mount a ConfigMap or Secret as files at a path. The deployer picks an existing resource
            or creates one at deploy time.
          </p>
          {form.volumeMounts.length ? (
            <div className="schema-env-list">
              {form.volumeMounts.map((row, index) => (
                <div key={index} className="schema-env-card">
                  <div className="schema-env-card__top">
                    <input
                      value={row.mountPath}
                      onChange={updateRow("volumeMounts", index, "mountPath")}
                      placeholder="/etc/config"
                      aria-label={`Volume ${index + 1} mount path`}
                      className="schema-env-card__key"
                    />
                    <label className="checkbox-row">
                      <input type="checkbox" checked={row.readOnly} onChange={updateRow("volumeMounts", index, "readOnly")} />
                      <span>Read-only</span>
                    </label>
                    <button
                      type="button"
                      className="btn-outline template-env-row__remove"
                      onClick={removeRow("volumeMounts", EMPTY_VOLUME, index)}
                      aria-label={`Remove volume ${index + 1}`}
                    >
                      ×
                    </button>
                  </div>
                  <div className="schema-env-card__sources" role="radiogroup" aria-label={`Volume ${index + 1} kind`}>
                    {VOLUME_KINDS.map((opt) => (
                      <label key={opt.value} className="schema-source-chip">
                        <input
                          type="radio"
                          name={`volume-kind-${index}`}
                          value={opt.value}
                          checked={row.kind === opt.value}
                          onChange={updateRow("volumeMounts", index, "kind")}
                        />
                        <span>{opt.label}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          <button type="button" className="btn-outline" onClick={addRow("volumeMounts", EMPTY_VOLUME)}>
            + Add volume mount
          </button>
        </section>

        <section className="form-section">
          <h4>Deployment overrides</h4>
          <p className="muted" style={{ marginTop: 0 }}>
            Choose what a deployer may change. Anything left unchecked is locked to the template defaults.
          </p>
          <div className="schema-override-grid">
            <label className="checkbox-row">
              <input type="checkbox" checked={form.overrides.image} onChange={toggleOverride("image")} />
              <span>Image</span>
            </label>
            <label className="checkbox-row">
              <input type="checkbox" checked={form.overrides.tag} onChange={toggleOverride("tag")} />
              <span>Image tag</span>
            </label>
            <label className="checkbox-row">
              <input type="checkbox" checked={form.overrides.replicas} onChange={toggleOverride("replicas")} />
              <span>Replicas</span>
            </label>
            <label className="checkbox-row">
              <input type="checkbox" checked={form.overrides.resources} onChange={toggleOverride("resources")} />
              <span>CPU / memory</span>
            </label>
            <label className="checkbox-row">
              <input type="checkbox" checked={form.overrides.storageSize} onChange={toggleOverride("storageSize")} disabled={!form.storageEnabled} />
              <span>Storage size</span>
            </label>
            <label className="checkbox-row">
              <input type="checkbox" checked={form.overrides.ingressHost} onChange={toggleOverride("ingressHost")} />
              <span>Ingress host</span>
            </label>
          </div>
          <div className="schema-svc-types">
            <span className="muted">Allowed service types (deployer may choose):</span>
            <div className="schema-env-card__sources">
              {SERVICE_TYPES.map((type) => (
                <label key={type} className="schema-source-chip">
                  <input type="checkbox" checked={(form.overrides.serviceTypes || []).includes(type)} onChange={() => toggleServiceType(type)} />
                  <span>{type}</span>
                </label>
              ))}
            </div>
          </div>
          {form.overrides.ingressHost ? (
            <label className="checkbox-row" style={{ marginTop: "var(--space-3)" }}>
              <input type="checkbox" checked={form.ingressTls} onChange={toggle("ingressTls")} />
              <span>Offer TLS on ingress (deployer supplies or creates the certificate secret)</span>
            </label>
          ) : null}
        </section>

        <section className="form-section">
          <h4>Health checks</h4>
          <p className="muted" style={{ marginTop: 0 }}>
            Optional liveness, readiness, and startup probes. HTTP/TCP probes default to the container port.
          </p>
          {PROBE_KEYS.map(({ key, label }) => {
            const probe = form.probes[key];
            return (
              <div key={key} className="template-probe">
                <label className="checkbox-row">
                  <input type="checkbox" checked={probe.enabled} onChange={updateProbe(key, "enabled")} />
                  <strong>{label}</strong>
                </label>
                {probe.enabled ? (
                  <div className="form-grid" style={{ marginTop: "var(--space-2)" }}>
                    <label>
                      Type
                      <select value={probe.type} onChange={updateProbe(key, "type")}>
                        {PROBE_TYPES.map((opt) => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </select>
                    </label>
                    {probe.type === "http" ? (
                      <label>
                        Path
                        <input value={probe.path} onChange={updateProbe(key, "path")} placeholder="/healthz" />
                      </label>
                    ) : null}
                    {probe.type === "http" || probe.type === "tcp" ? (
                      <label>
                        Port
                        <input value={probe.port} onChange={updateProbe(key, "port")} inputMode="numeric" placeholder="(container port)" />
                      </label>
                    ) : null}
                    {probe.type === "command" ? (
                      <label className="form-grid__full">
                        Command
                        <input value={probe.command} onChange={updateProbe(key, "command")} placeholder="e.g. cat /tmp/healthy" />
                      </label>
                    ) : null}
                    <label>
                      Initial delay (s)
                      <input value={probe.initialDelaySeconds} onChange={updateProbe(key, "initialDelaySeconds")} inputMode="numeric" />
                    </label>
                    <label>
                      Period (s)
                      <input value={probe.periodSeconds} onChange={updateProbe(key, "periodSeconds")} inputMode="numeric" />
                    </label>
                  </div>
                ) : null}
              </div>
            );
          })}
        </section>

        <section className="form-section">
          <h4>Resources</h4>
          <div className="form-grid">
            <label>
              CPU request
              <input value={form.cpuRequest} onChange={update("cpuRequest")} placeholder="100m" />
            </label>
            <label>
              CPU limit
              <input value={form.cpuLimit} onChange={update("cpuLimit")} placeholder="500m" />
            </label>
            <label>
              Memory request
              <input value={form.memoryRequest} onChange={update("memoryRequest")} placeholder="128Mi" />
            </label>
            <label>
              Memory limit
              <input value={form.memoryLimit} onChange={update("memoryLimit")} placeholder="256Mi" />
            </label>
          </div>
        </section>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          {isEditing ? (
            <button
              type="button"
              className="btn-outline"
              onClick={() => handleSubmit(true)}
              disabled={saving}
              title="Save these changes as a new version, leaving the original untouched"
            >
              {pendingAction === "version" ? "Saving…" : "Save as New Version"}
            </button>
          ) : null}
          <button type="button" className="primary" onClick={() => handleSubmit(false)} disabled={saving}>
            {pendingAction === "save" ? "Saving…" : isEditing ? "Save Changes" : "Create Template"}
          </button>
        </div>
      </div>
    </div>
  );
}
