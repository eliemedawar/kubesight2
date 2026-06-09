import { useEffect, useMemo, useState } from "react";
import {
  applyDeployImage,
  applyDeployYaml,
  diffDeployYaml,
  dryRunDeployImage,
  dryRunDeployYaml,
  generateImageManifests,
  listInventoryWorkloads,
  registerExistingApp,
  validateDeployYaml,
} from "../../api/inventoryApi.js";
import { useAuth } from "../../context/AuthContext.jsx";
import { formatAccessError, isAccessDeniedError } from "../../utils/authz.js";
import HelmDeployForm from "./HelmDeployForm.jsx";
import NamespaceSelect from "./NamespaceSelect.jsx";
import { clusterOptionLabel, normalizeClusterOptions } from "../../utils/clusterOptions.js";

function describeDeployDiffError(err) {
  const message = err?.message || "Diff preview is unavailable.";
  if (isAccessDeniedError(message)) {
    return "Diff preview requires the apps:diff permission. You can still apply after confirming below.";
  }
  const lowered = message.toLowerCase();
  if (
    lowered.includes("executable file not found") ||
    lowered.includes('failed to run "diff"') ||
    lowered.includes("diff utility")
  ) {
    return message;
  }
  return formatAccessError(message, { suppressAccessDenied: false }) || message;
}

const CRITICALITY_OPTIONS = ["Low", "Medium", "High", "Critical"];
const ENVIRONMENT_OPTIONS = ["Development", "Staging", "Production", "DR"];
const SERVICE_TYPES = ["ClusterIP", "NodePort", "LoadBalancer"];

const EMPTY_REGISTER = {
  clusterId: "",
  namespace: "",
  workloadType: "",
  workloadName: "",
  displayName: "",
  ownerTeam: "",
  environment: "",
  criticality: "",
  description: "",
  documentationUrl: "",
  contactEmail: "",
  tags: "",
};

const EMPTY_YAML = {
  clusterId: "",
  namespace: "",
  yaml: "",
  deploymentName: "",
  description: "",
};

const EMPTY_IMAGE = {
  clusterId: "",
  namespace: "",
  appName: "",
  dockerImage: "",
  imageTag: "latest",
  replicas: 1,
  containerPort: 8080,
  serviceType: "ClusterIP",
  cpuRequest: "100m",
  cpuLimit: "500m",
  memoryRequest: "128Mi",
  memoryLimit: "512Mi",
  ownerTeam: "",
  environment: "",
  criticality: "",
  description: "",
  contactEmail: "",
  envVars: "",
};

function parseEnvVars(text) {
  const result = {};
  for (const line of String(text || "").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || !trimmed.includes("=")) continue;
    const [key, ...rest] = trimmed.split("=");
    result[key.trim()] = rest.join("=").trim();
  }
  return result;
}

export default function AddAppModal({
  open,
  onClose,
  onSuccess,
  clusterOptions = [],
  defaultClusterId = "",
  canRegister = false,
  canDeploy = false,
  canHelmInstall = false,
  onOpenWizard,
}) {
  const { hasPermission } = useAuth();
  const canViewDeployDiff = hasPermission("apps:diff");
  const clusterSelectOptions = normalizeClusterOptions(clusterOptions);
  const resolvedDefaultClusterId =
    defaultClusterId &&
    clusterSelectOptions.some((cluster) => cluster.id === defaultClusterId)
      ? defaultClusterId
      : clusterSelectOptions[0]?.id || "";
  const [step, setStep] = useState("choose");
  const [mode, setMode] = useState("");
  const [registerForm, setRegisterForm] = useState(EMPTY_REGISTER);
  const [yamlForm, setYamlForm] = useState(EMPTY_YAML);
  const [imageForm, setImageForm] = useState(EMPTY_IMAGE);
  const [workloads, setWorkloads] = useState([]);
  const [workloadsLoading, setWorkloadsLoading] = useState(false);
  const [preview, setPreview] = useState(null);
  /** @type {null | { content: string } | { hint: string }} */
  const [deployDiff, setDeployDiff] = useState(null);
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setStep("choose");
      setMode("");
      setRegisterForm(EMPTY_REGISTER);
      setYamlForm(EMPTY_YAML);
      setImageForm(EMPTY_IMAGE);
      setWorkloads([]);
      setPreview(null);
      setDeployDiff(null);
      setConfirmation("");
      setError("");
    }
  }, [open]);

  useEffect(() => {
    if (!open || !resolvedDefaultClusterId) {
      return;
    }
    setRegisterForm((prev) =>
      prev.clusterId ? prev : { ...prev, clusterId: resolvedDefaultClusterId }
    );
    setYamlForm((prev) =>
      prev.clusterId ? prev : { ...prev, clusterId: resolvedDefaultClusterId }
    );
    setImageForm((prev) =>
      prev.clusterId ? prev : { ...prev, clusterId: resolvedDefaultClusterId }
    );
  }, [open, resolvedDefaultClusterId]);

  useEffect(() => {
    if (mode !== "register" || !registerForm.clusterId || !registerForm.namespace) {
      return;
    }
    let cancelled = false;
    setWorkloadsLoading(true);
    listInventoryWorkloads({
      clusterId: registerForm.clusterId,
      namespace: registerForm.namespace,
    })
      .then((items) => {
        if (!cancelled) setWorkloads(items || []);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Failed to load workloads");
      })
      .finally(() => {
        if (!cancelled) setWorkloadsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, registerForm.clusterId, registerForm.namespace]);

  const confirmationPhrase = useMemo(() => {
    const ns = yamlForm.namespace || imageForm.namespace;
    return ns ? `APPLY ${ns}` : "";
  }, [yamlForm.namespace, imageForm.namespace]);

  if (!open) return null;

  const resetFlow = () => {
    setStep("choose");
    setMode("");
    setPreview(null);
    setDeployDiff(null);
    setConfirmation("");
    setError("");
  };

  const handleWorkloadSelect = (value) => {
    const [type, name] = value.split("|");
    const wl = workloads.find((w) => w.type === type && w.name === name);
    setRegisterForm((prev) => ({
      ...prev,
      workloadType: type,
      workloadName: name,
      displayName: prev.displayName || name,
    }));
    if (wl?.labels?.["app.kubernetes.io/name"]) {
      setRegisterForm((prev) => ({
        ...prev,
        displayName: wl.labels["app.kubernetes.io/name"],
      }));
    }
  };

  const submitRegister = async () => {
    setBusy(true);
    setError("");
    try {
      await registerExistingApp({
        ...registerForm,
        tags: registerForm.tags
          ? registerForm.tags.split(",").map((t) => t.trim()).filter(Boolean)
          : [],
      });
      onSuccess?.();
      onClose();
    } catch (err) {
      setError(err.message || "Registration failed");
    } finally {
      setBusy(false);
    }
  };

  const runYamlPreview = async () => {
    setBusy(true);
    setError("");
    try {
      const validation = await validateDeployYaml({
        clusterId: yamlForm.clusterId,
        namespace: yamlForm.namespace,
        yaml: yamlForm.yaml,
      });
      const dryRun = await dryRunDeployYaml({
        clusterId: yamlForm.clusterId,
        namespace: yamlForm.namespace,
        yaml: yamlForm.yaml,
      });
      if (!canViewDeployDiff) {
        setDeployDiff({
          hint: "Diff preview requires the apps:diff permission. You can still apply after confirming below.",
        });
      } else {
        try {
          const diffPayload = await diffDeployYaml({
            clusterId: yamlForm.clusterId,
            namespace: yamlForm.namespace,
            yaml: yamlForm.yaml,
          });
          const content = String(diffPayload.diff || "").trim();
          setDeployDiff(content ? { content } : null);
        } catch (diffError) {
          setDeployDiff({ hint: describeDeployDiffError(diffError) });
        }
      }
      setPreview({ validation, dryRun });
      setStep("yaml-confirm");
    } catch (err) {
      setError(err.message || "Validation failed");
    } finally {
      setBusy(false);
    }
  };

  const applyYaml = async () => {
    setBusy(true);
    setError("");
    try {
      await applyDeployYaml({
        clusterId: yamlForm.clusterId,
        namespace: yamlForm.namespace,
        yaml: yamlForm.yaml,
        confirmation,
        deploymentName: yamlForm.deploymentName,
        description: yamlForm.description,
      });
      onSuccess?.();
      onClose();
    } catch (err) {
      setError(err.message || "Apply failed");
    } finally {
      setBusy(false);
    }
  };

  const runImagePreview = async () => {
    setBusy(true);
    setError("");
    try {
      const generated = await generateImageManifests({
        ...imageForm,
        environmentVariables: parseEnvVars(imageForm.envVars),
      });
      const dryRun = await dryRunDeployImage({
        ...imageForm,
        environmentVariables: parseEnvVars(imageForm.envVars),
      });
      setPreview({ generated, dryRun, yaml: generated.yaml || dryRun.yaml });
      setStep("image-confirm");
    } catch (err) {
      setError(err.message || "Preview failed");
    } finally {
      setBusy(false);
    }
  };

  const applyImage = async () => {
    setBusy(true);
    setError("");
    try {
      await applyDeployImage({
        ...imageForm,
        environmentVariables: parseEnvVars(imageForm.envVars),
        confirmation,
        tags: [],
      });
      onSuccess?.();
      onClose();
    } catch (err) {
      setError(err.message || "Apply failed");
    } finally {
      setBusy(false);
    }
  };

  const handleYamlFile = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      setYamlForm((prev) => ({ ...prev, yaml: String(reader.result || "") }));
    };
    reader.readAsText(file);
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel add-app-modal"
        role="dialog"
        aria-labelledby="add-app-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3 id="add-app-title">Add Application</h3>
            <p className="muted">Register an existing workload or deploy a new application.</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        {error ? <p className="banner-message error">{error}</p> : null}

        {step === "choose" ? (
          <div className="add-app-choices">
            {canRegister ? (
              <button
                type="button"
                className="btn-primary add-app-choice"
                onClick={() => {
                  setMode("register");
                  setStep("register");
                }}
              >
                Register Existing App
                <span className="muted">Add catalog metadata for a running workload</span>
              </button>
            ) : null}
            {canDeploy ? (
              <button
                type="button"
                className="btn-primary add-app-choice"
                onClick={() => {
                  onClose();
                  onOpenWizard?.();
                }}
              >
                Application Builder
                <span className="muted">Guided wizard — deploy without writing YAML</span>
              </button>
            ) : null}
            {(canDeploy || canHelmInstall) ? (
              <button
                type="button"
                className="btn-outline add-app-choice"
                onClick={() => {
                  setMode("deploy");
                  setStep("deploy-choose");
                }}
              >
                Advanced Deploy
                <span className="muted">YAML, Docker image, or Helm chart</span>
              </button>
            ) : null}
          </div>
        ) : null}

        {step === "deploy-choose" ? (
          <div className="add-app-choices">
            {canDeploy ? (
              <>
                <button type="button" className="btn-outline" onClick={() => setStep("yaml-form")}>
                  Deploy from Kubernetes YAML
                </button>
                <button type="button" className="btn-outline" onClick={() => setStep("image-form")}>
                  Deploy from Docker Image
                </button>
              </>
            ) : null}
            {canHelmInstall ? (
              <button type="button" className="btn-outline" onClick={() => setStep("helm-deploy")}>
                Deploy from Helm Chart
              </button>
            ) : null}
            <button type="button" className="btn-text" onClick={resetFlow}>
              Back
            </button>
          </div>
        ) : null}

        {step === "helm-deploy" ? (
          <HelmDeployForm
            clusterOptions={clusterSelectOptions}
            defaultClusterId={defaultClusterId}
            onBack={() => setStep("deploy-choose")}
            onSuccess={() => {
              onSuccess?.();
              onClose();
            }}
            onCancel={onClose}
          />
        ) : null}

        {step === "register" ? (
          <form
            className="add-app-form"
            onSubmit={(e) => {
              e.preventDefault();
              submitRegister();
            }}
          >
            <label>
              Cluster
              <select
                required
                value={registerForm.clusterId}
                onChange={(e) =>
                  setRegisterForm((p) => ({
                    ...p,
                    clusterId: e.target.value,
                    namespace: "",
                    workloadType: "",
                    workloadName: "",
                  }))
                }
              >
                <option value="">Select cluster</option>
                {clusterSelectOptions.map((cluster) => (
                  <option key={cluster.id} value={cluster.id}>
                    {clusterOptionLabel(cluster)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Namespace
              <NamespaceSelect
                clusterId={registerForm.clusterId}
                value={registerForm.namespace}
                onChange={(e) =>
                  setRegisterForm((p) => ({
                    ...p,
                    namespace: e.target.value,
                    workloadType: "",
                    workloadName: "",
                  }))
                }
              />
            </label>
            <label>
              Detected Workload
              <select
                required
                value={
                  registerForm.workloadType && registerForm.workloadName
                    ? `${registerForm.workloadType}|${registerForm.workloadName}`
                    : ""
                }
                onChange={(e) => handleWorkloadSelect(e.target.value)}
                disabled={workloadsLoading}
              >
                <option value="">
                  {workloadsLoading ? "Loading workloads..." : "Select workload"}
                </option>
                {workloads.map((wl) => (
                  <option key={`${wl.type}|${wl.name}`} value={`${wl.type}|${wl.name}`}>
                    {wl.type} / {wl.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              App Display Name
              <input
                required
                value={registerForm.displayName}
                onChange={(e) => setRegisterForm((p) => ({ ...p, displayName: e.target.value }))}
              />
            </label>
            <label>
              Owner / Team
              <input
                value={registerForm.ownerTeam}
                onChange={(e) => setRegisterForm((p) => ({ ...p, ownerTeam: e.target.value }))}
              />
            </label>
            <label>
              Environment
              <select
                value={registerForm.environment}
                onChange={(e) => setRegisterForm((p) => ({ ...p, environment: e.target.value }))}
              >
                <option value="">Not set</option>
                {ENVIRONMENT_OPTIONS.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            </label>
            <label>
              Criticality
              <select
                value={registerForm.criticality}
                onChange={(e) => setRegisterForm((p) => ({ ...p, criticality: e.target.value }))}
              >
                <option value="">Not set</option>
                {CRITICALITY_OPTIONS.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            </label>
            <label>
              Description
              <textarea
                rows={2}
                value={registerForm.description}
                onChange={(e) => setRegisterForm((p) => ({ ...p, description: e.target.value }))}
              />
            </label>
            <label>
              Documentation URL
              <input
                type="url"
                value={registerForm.documentationUrl}
                onChange={(e) => setRegisterForm((p) => ({ ...p, documentationUrl: e.target.value }))}
              />
            </label>
            <label>
              Contact Email
              <input
                type="email"
                value={registerForm.contactEmail}
                onChange={(e) => setRegisterForm((p) => ({ ...p, contactEmail: e.target.value }))}
              />
            </label>
            <label>
              Tags (comma-separated)
              <input
                value={registerForm.tags}
                onChange={(e) => setRegisterForm((p) => ({ ...p, tags: e.target.value }))}
              />
            </label>
            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={resetFlow}>Back</button>
              <button type="submit" className="btn-primary" disabled={busy}>
                {busy ? "Registering..." : "Register App"}
              </button>
            </div>
          </form>
        ) : null}

        {step === "yaml-form" ? (
          <form
            className="add-app-form"
            onSubmit={(e) => {
              e.preventDefault();
              runYamlPreview();
            }}
          >
            <label>
              Cluster
              <select
                required
                value={yamlForm.clusterId}
                onChange={(e) =>
                  setYamlForm((p) => ({ ...p, clusterId: e.target.value, namespace: "" }))
                }
              >
                <option value="">Select cluster</option>
                {clusterSelectOptions.map((cluster) => (
                  <option key={cluster.id} value={cluster.id}>
                    {clusterOptionLabel(cluster)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Namespace
              <NamespaceSelect
                clusterId={yamlForm.clusterId}
                value={yamlForm.namespace}
                onChange={(e) => setYamlForm((p) => ({ ...p, namespace: e.target.value }))}
              />
            </label>
            <label>
              Kubernetes YAML
              <textarea
                required
                rows={12}
                className="yaml-editor"
                value={yamlForm.yaml}
                onChange={(e) => setYamlForm((p) => ({ ...p, yaml: e.target.value }))}
                placeholder="Paste Deployment, Service, ConfigMap YAML..."
              />
            </label>
            <label>
              Upload YAML file
              <input type="file" accept=".yaml,.yml" onChange={handleYamlFile} />
            </label>
            <label>
              Deployment name override (optional)
              <input
                value={yamlForm.deploymentName}
                onChange={(e) => setYamlForm((p) => ({ ...p, deploymentName: e.target.value }))}
              />
            </label>
            <label>
              Description (optional)
              <input
                value={yamlForm.description}
                onChange={(e) => setYamlForm((p) => ({ ...p, description: e.target.value }))}
              />
            </label>
            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={() => setStep("deploy-choose")}>Back</button>
              <button type="submit" className="btn-primary" disabled={busy}>
                {busy ? "Validating..." : "Validate & Preview"}
              </button>
            </div>
          </form>
        ) : null}

        {step === "yaml-confirm" && preview ? (
          <div className="deploy-preview">
            <h4>Resources to create/update</h4>
            <ul>
              {(preview.dryRun?.resources || preview.validation?.resources || []).map((res) => (
                <li key={`${res.kind}-${res.name}`}>
                  {res.kind} {res.name}
                </li>
              ))}
            </ul>
            {(preview.validation?.warnings || []).length ? (
              <div className="banner-message warn">
                {(preview.validation.warnings || []).join("; ")}
              </div>
            ) : null}
            {deployDiff?.content ? (
              <>
                <h4>Diff</h4>
                <p className="muted deploy-diff-caption">
                  Changes compared to what is already running in the cluster.
                </p>
                <pre className="yaml-preview">{deployDiff.content}</pre>
              </>
            ) : null}
            {deployDiff?.hint ? (
              <p className="muted deploy-diff-hint">{deployDiff.hint}</p>
            ) : null}
            <label>
              Type <strong>{confirmationPhrase}</strong> to confirm
              <input
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                placeholder={confirmationPhrase}
              />
            </label>
            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={() => setStep("yaml-form")}>Back</button>
              <button
                type="button"
                className="btn-primary"
                disabled={busy || confirmation !== confirmationPhrase}
                onClick={applyYaml}
              >
                {busy ? "Applying..." : "Apply to Cluster"}
              </button>
            </div>
          </div>
        ) : null}

        {step === "image-form" ? (
          <form
            className="add-app-form add-app-form-grid"
            onSubmit={(e) => {
              e.preventDefault();
              runImagePreview();
            }}
          >
            <label>
              Cluster
              <select
                required
                value={imageForm.clusterId}
                onChange={(e) =>
                  setImageForm((p) => ({ ...p, clusterId: e.target.value, namespace: "" }))
                }
              >
                <option value="">Select</option>
                {clusterSelectOptions.map((cluster) => (
                  <option key={cluster.id} value={cluster.id}>
                    {clusterOptionLabel(cluster)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Namespace
              <NamespaceSelect
                clusterId={imageForm.clusterId}
                value={imageForm.namespace}
                onChange={(e) => setImageForm((p) => ({ ...p, namespace: e.target.value }))}
              />
            </label>
            <label>App Name<input required value={imageForm.appName} onChange={(e) => setImageForm((p) => ({ ...p, appName: e.target.value }))} /></label>
            <label>Docker Image<input required value={imageForm.dockerImage} onChange={(e) => setImageForm((p) => ({ ...p, dockerImage: e.target.value }))} placeholder="nginx" /></label>
            <label>Image Tag<input value={imageForm.imageTag} onChange={(e) => setImageForm((p) => ({ ...p, imageTag: e.target.value }))} /></label>
            <label>Replicas<input type="number" min="0" value={imageForm.replicas} onChange={(e) => setImageForm((p) => ({ ...p, replicas: Number(e.target.value) }))} /></label>
            <label>Container Port<input type="number" value={imageForm.containerPort} onChange={(e) => setImageForm((p) => ({ ...p, containerPort: Number(e.target.value) }))} /></label>
            <label>Service Type<select value={imageForm.serviceType} onChange={(e) => setImageForm((p) => ({ ...p, serviceType: e.target.value }))}>{SERVICE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
            <label>CPU Request<input value={imageForm.cpuRequest} onChange={(e) => setImageForm((p) => ({ ...p, cpuRequest: e.target.value }))} /></label>
            <label>CPU Limit<input value={imageForm.cpuLimit} onChange={(e) => setImageForm((p) => ({ ...p, cpuLimit: e.target.value }))} /></label>
            <label>Memory Request<input value={imageForm.memoryRequest} onChange={(e) => setImageForm((p) => ({ ...p, memoryRequest: e.target.value }))} /></label>
            <label>Memory Limit<input value={imageForm.memoryLimit} onChange={(e) => setImageForm((p) => ({ ...p, memoryLimit: e.target.value }))} /></label>
            <label className="full-width">Environment Variables (KEY=value per line)<textarea rows={3} value={imageForm.envVars} onChange={(e) => setImageForm((p) => ({ ...p, envVars: e.target.value }))} /></label>
            <label>Owner / Team<input value={imageForm.ownerTeam} onChange={(e) => setImageForm((p) => ({ ...p, ownerTeam: e.target.value }))} /></label>
            <label>Environment<select value={imageForm.environment} onChange={(e) => setImageForm((p) => ({ ...p, environment: e.target.value }))}><option value="">Not set</option>{ENVIRONMENT_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}</select></label>
            <label>Criticality<select value={imageForm.criticality} onChange={(e) => setImageForm((p) => ({ ...p, criticality: e.target.value }))}><option value="">Not set</option>{CRITICALITY_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}</select></label>
            <label className="full-width">Description<textarea rows={2} value={imageForm.description} onChange={(e) => setImageForm((p) => ({ ...p, description: e.target.value }))} /></label>
            <div className="modal-actions full-width">
              <button type="button" className="btn-text" onClick={() => setStep("deploy-choose")}>Back</button>
              <button type="submit" className="btn-primary" disabled={busy}>{busy ? "Generating..." : "Generate & Preview"}</button>
            </div>
          </form>
        ) : null}

        {step === "image-confirm" && preview ? (
          <div className="deploy-preview">
            <h4>Generated YAML</h4>
            <pre className="yaml-preview">{preview.yaml}</pre>
            <h4>Resources to create</h4>
            <ul>
              {(preview.dryRun?.resources || []).map((res) => (
                <li key={`${res.kind}-${res.name}`}>{res.kind} {res.name}</li>
              ))}
            </ul>
            <label>
              Type <strong>{confirmationPhrase}</strong> to confirm
              <input value={confirmation} onChange={(e) => setConfirmation(e.target.value)} placeholder={confirmationPhrase} />
            </label>
            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={() => setStep("image-form")}>Back</button>
              <button type="button" className="btn-primary" disabled={busy || confirmation !== confirmationPhrase} onClick={applyImage}>
                {busy ? "Applying..." : "Apply to Cluster"}
              </button>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
