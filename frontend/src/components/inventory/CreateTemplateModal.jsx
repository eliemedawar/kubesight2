import { useEffect, useMemo, useRef, useState } from "react";
import SearchableSelect from "../common/SearchableSelect.jsx";
import useEscapeKey from "../../hooks/useEscapeKey.js";
import {
  EMPTY_DEPENDENCY,
  EMPTY_ENV,
  EMPTY_FORM,
  EMPTY_STORAGE_VOLUME,
  EMPTY_VOLUME,
  NEW_CATEGORY,
  PROBE_KEYS,
  describeKeptExtras,
  formFromTemplate,
  freshForm,
  hasHpaResources as formHasHpaResources,
  joinExtras,
  mergeTemplateEdit,
  validateTemplateForm,
} from "./templateFormModel.js";

const WORKLOAD_TYPES = ["Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"];
const PULL_POLICIES = ["IfNotPresent", "Always", "Never"];

// The author picks a single *kind* per variable. The deployer later chooses
// "existing vs create" for ConfigMap/Secret kinds. Sensitive forces Secret.
const ENV_KINDS = [
  { value: "value", label: "Plain value" },
  { value: "configMap", label: "ConfigMap" },
  { value: "secret", label: "Secret" },
];

// A volume mount sources a whole ConfigMap or Secret as files on disk. The author
// picks the kind; the deployer later chooses existing-vs-create at deploy time.
const VOLUME_KINDS = [
  { value: "configMap", label: "ConfigMap" },
  { value: "secret", label: "Secret" },
];

const DEPENDENCY_KINDS = ["postgresql", "redis", "kafka", "rabbitmq", "minio", "service"];
const PROBE_TYPES = [
  { value: "http", label: "HTTP GET" },
  { value: "tcp", label: "TCP socket" },
  { value: "command", label: "Command" },
];

export default function CreateTemplateModal({
  open,
  existingCategories = [],
  template = null,
  draft = null,
  onClose,
  onSubmit,
}) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  // Which button is mid-flight, so we can label just that one "Saving…".
  const [pendingAction, setPendingAction] = useState("");
  const [showCloseConfirm, setShowCloseConfirm] = useState(false);
  const isEditing = Boolean(template);
  // The form as it was opened, so closing an untouched form needs no confirm.
  const initialFormRef = useRef("");
  const errorRef = useRef(null);
  // What the template being edited (or the import draft) holds beyond what this
  // form shows. Saving keeps all of it; the notice says so.
  const keptExtras = useMemo(() => describeKeptExtras(template || draft), [template, draft]);
  // A stored NodePort/LoadBalancer Service keeps its type when saved; say so.
  const serviceType = (template || draft)?.networking?.service?.type || "ClusterIP";

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

  // Read through a ref so a parent re-render that rebuilds the category list
  // does not re-run the reset below and wipe what the user has typed.
  const categoryOptionsRef = useRef(categoryOptions);
  useEffect(() => {
    categoryOptionsRef.current = categoryOptions;
  }, [categoryOptions]);

  useEffect(() => {
    if (open) {
      // `template` => edit/update an existing template; `draft` => prefill a brand
      // new template (e.g. parsed from imported YAML) while staying in create mode.
      const prefill = template || draft;
      const next = prefill
        ? formFromTemplate(prefill)
        : freshForm(categoryOptionsRef.current[0] || NEW_CATEGORY);
      setForm(next);
      initialFormRef.current = JSON.stringify(next);
      setError("");
      setSaving(false);
      setPendingAction("");
      setShowCloseConfirm(false);
    }
  }, [open, template, draft]);

  // Errors render at the top of a long form; bring them into view when they
  // appear after a click on the footer buttons.
  useEffect(() => {
    if (error) errorRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [error]);

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

  // Closing discards the in-progress template, so a user-initiated dismissal
  // (backdrop click, Escape or Cancel) asks for confirmation first when anything
  // was changed. Programmatic closes after a successful save call onClose
  // directly and skip this.
  const requestClose = () => {
    if (saving) return;
    if (JSON.stringify(form) === initialFormRef.current) {
      onClose();
      return;
    }
    setShowCloseConfirm(true);
  };
  const confirmClose = () => {
    setShowCloseConfirm(false);
    onClose();
  };
  const cancelClose = () => setShowCloseConfirm(false);

  useEscapeKey(open, () => (showCloseConfirm ? cancelClose() : requestClose()));

  if (!open) return null;

  const update = (key) => (event) => setForm((prev) => ({ ...prev, [key]: event.target.value }));
  const toggle = (key) => (event) => setForm((prev) => ({ ...prev, [key]: event.target.checked }));

  // HPA can only be enabled when both CPU and memory requests are defined,
  // since autoscaling computes utilization relative to those requests.
  const hasHpaResources = formHasHpaResources(form);

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

  // `asNewVersion` forks the edited template into a fresh "vN" copy instead of
  // overwriting the original — the parent decides the new name and calls create.
  const handleSubmit = async (asNewVersion = false) => {
    setError("");
    const problem = validateTemplateForm(form);
    if (problem) {
      setError(problem);
      return;
    }
    // Start from the stored template (or import draft) and apply only what was
    // edited here, so the parts this form can't show are saved unchanged.
    const stored = template || draft;
    const payload = mergeTemplateEdit(stored, stored ? JSON.parse(initialFormRef.current) : null, form);

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
    <div className="modal-backdrop" role="presentation" onClick={requestClose}>
      <div
        className="modal-card modal-card--wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-template-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3 id="create-template-title">{isEditing ? "Edit Template" : "Create Template"}</h3>
          <p className="muted">
            Define a reusable <strong>schema</strong> — what a deployment requires, what it may override, and what stays
            locked. Actual values are supplied at deploy time, never stored here.
          </p>
        </div>

        {error ? (
          <p ref={errorRef} className="banner-message error" role="alert">
            {error}
          </p>
        ) : null}

        {keptExtras.length ? (
          <p className="banner-message info template-kept-note" role="note">
            This template also has {joinExtras(keptExtras)} that this form doesn&apos;t edit; they are kept as
            they are.
          </p>
        ) : null}

        <section className="form-section">
          <h4>Details</h4>
          <div className="form-grid">
            <label>
              Template name
              <input value={form.name} onChange={update("name")} placeholder="e.g. Internal API" />
            </label>
            <label>
              Workload type
              <SearchableSelect value={form.workloadType} onChange={update("workloadType")}>
                {WORKLOAD_TYPES.map((type) => (
                  <option key={type} value={type}>{type}</option>
                ))}
              </SearchableSelect>
            </label>
            <label className="form-grid__full">
              Description
              <input value={form.description} onChange={update("description")} placeholder="What this template deploys" />
            </label>
            <label>
              Category
              <SearchableSelect value={form.category} onChange={update("category")}>
                {categoryOptions.map((category) => (
                  <option key={category} value={category}>{category}</option>
                ))}
                <option value={NEW_CATEGORY}>+ New category…</option>
              </SearchableSelect>
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
              <SearchableSelect value={form.pullPolicy} onChange={update("pullPolicy")}>
                {PULL_POLICIES.map((policy) => (
                  <option key={policy} value={policy}>{policy}</option>
                ))}
              </SearchableSelect>
            </label>
            <label className="checkbox-row form-grid__full">
              <input type="checkbox" checked={form.serviceEnabled} onChange={toggle("serviceEnabled")} />
              <span>Expose a {serviceType} service on the container port</span>
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
                <div className="schema-env-card__sources">
                  <SearchableSelect
                    value={row.sensitive ? "secret" : row.kind}
                    disabled={row.sensitive}
                    onChange={(e) => setEnvKind(index, e.target.value)}
                    aria-label={`Variable ${index + 1} kind`}
                    className="schema-env-card__kind"
                  >
                    {ENV_KINDS.map((opt) => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </SearchableSelect>
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
                    <SearchableSelect value={dep.kind} onChange={updateDependency(depIndex, "kind")} aria-label={`Dependency ${depIndex + 1} kind`}>
                      {DEPENDENCY_KINDS.map((kind) => (
                        <option key={kind} value={kind}>{kind}</option>
                      ))}
                    </SearchableSelect>
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
            <>
              <p className="muted" style={{ marginTop: "var(--space-2)" }}>
                Each volume becomes its own PVC mounted at the given path (e.g. /app/pin and /app/logs).
              </p>
              <div className="schema-env-list" style={{ marginTop: "var(--space-3)" }}>
                {form.storageVolumes.map((vol, index) => (
                  <div key={index} className="schema-env-card">
                    <div className="form-grid">
                      <label>
                        Mount path
                        <input
                          value={vol.mountPath}
                          onChange={updateRow("storageVolumes", index, "mountPath")}
                          placeholder={index === 0 ? "/data" : "/app/logs"}
                          aria-label={`Storage volume ${index + 1} mount path`}
                        />
                      </label>
                      <label>
                        Default size
                        <input
                          value={vol.size}
                          onChange={updateRow("storageVolumes", index, "size")}
                          placeholder="1Gi"
                          aria-label={`Storage volume ${index + 1} size`}
                        />
                      </label>
                    </div>
                    {form.storageVolumes.length > 1 ? (
                      <button
                        type="button"
                        className="btn-outline template-env-row__remove"
                        onClick={removeRow("storageVolumes", EMPTY_STORAGE_VOLUME, index)}
                        aria-label={`Remove storage volume ${index + 1}`}
                      >
                        ×
                      </button>
                    ) : null}
                  </div>
                ))}
              </div>
              <button type="button" className="btn-outline" onClick={addRow("storageVolumes", EMPTY_STORAGE_VOLUME)}>
                + Add volume
              </button>
              <label className="checkbox-row" style={{ marginTop: "var(--space-3)" }}>
                <input type="checkbox" checked={form.overrides.storageSize} onChange={toggleOverride("storageSize")} />
                <span>Allow the deployer to override the size</span>
              </label>
            </>
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
                  <input
                    value={row.subPath || ""}
                    onChange={updateRow("volumeMounts", index, "subPath")}
                    placeholder="subPath (optional) — mount a single key as this file"
                    aria-label={`Volume ${index + 1} subPath`}
                    className="schema-env-card__default"
                  />
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
                      <SearchableSelect value={probe.type} onChange={updateProbe(key, "type")}>
                        {PROBE_TYPES.map((opt) => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </SearchableSelect>
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

        {form.workloadType === "Deployment" || form.workloadType === "StatefulSet" ? (
          <section className="form-section">
            <h4>Autoscaling (HPA)</h4>
            <label className={`wizard-checkbox${hasHpaResources ? "" : " wizard-checkbox--disabled"}`}>
              <input
                type="checkbox"
                checked={form.hpaEnabled && hasHpaResources}
                disabled={!hasHpaResources}
                onChange={toggle("hpaEnabled")}
              />
              Enable Horizontal Pod Autoscaler
            </label>
            {!hasHpaResources ? (
              <p className="wizard-hpa-warning">
                ⚠️ HPA requires CPU and Memory requests to be defined above.
              </p>
            ) : null}
            {form.hpaEnabled && hasHpaResources ? (
              <>
                <div className="form-grid">
                  <label>
                    Min replicas
                    <input value={form.hpaMinReplicas} onChange={update("hpaMinReplicas")} inputMode="numeric" placeholder="1" />
                  </label>
                  <label>
                    Max replicas
                    <input value={form.hpaMaxReplicas} onChange={update("hpaMaxReplicas")} inputMode="numeric" placeholder="5" />
                  </label>
                  <label>
                    CPU threshold %
                    <input value={form.hpaCpuThreshold} onChange={update("hpaCpuThreshold")} inputMode="numeric" placeholder="optional" />
                  </label>
                  <label>
                    Memory threshold %
                    <input value={form.hpaMemoryThreshold} onChange={update("hpaMemoryThreshold")} inputMode="numeric" placeholder="optional" />
                  </label>
                </div>
                <p className="muted" style={{ margin: "0.25rem 0 0" }}>
                  Scale on CPU, memory, or both — set at least one threshold.
                </p>
              </>
            ) : null}
          </section>
        ) : null}

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={requestClose} disabled={saving}>
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

      {showCloseConfirm ? (
        <div
          className="modal-backdrop template-close-confirm"
          role="presentation"
          onClick={(e) => {
            e.stopPropagation();
            cancelClose();
          }}
        >
          <div
            className="modal-card template-close-confirm__panel"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="template-close-confirm-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-card__header">
              <h3 id="template-close-confirm-title">
                Discard this {isEditing ? "edit" : "template"}?
              </h3>
              <p className="muted">
                You have unsaved changes. Closing now will discard everything
                you&apos;ve entered.
              </p>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn-outline" onClick={cancelClose}>
                Keep editing
              </button>
              <button type="button" className="primary" onClick={confirmClose}>
                Discard &amp; close
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
