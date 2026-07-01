import { useEffect, useMemo, useState } from "react";

import { applyWizardDeploy, resolveWizardTemplate } from "../../../api/inventoryApi.js";
import {
  listNamespaceConfigResources,
  listStorageClasses,
} from "../../../api/clustersApi.js";
import { getClusterDeployEligibility } from "../../../api/deploymentRequestsApi.js";
import { normalizeClusterOptions } from "../../../utils/clusterOptions.js";
import YamlPreviewPanel from "./YamlPreviewPanel.jsx";
import SearchableSelect from "../../common/SearchableSelect.jsx";
import NamespaceSelect from "../NamespaceSelect.jsx";
import AddToBundleButton from "../../changes/AddToBundleButton.jsx";

const POD_KINDS = new Set(["Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"]);
// A Service makes sense for long-running, selector-addressable workloads. Jobs and
// CronJobs run to completion and aren't fronted by a Service.
const SERVICE_WORKLOADS = new Set(["Deployment", "StatefulSet", "DaemonSet"]);
const SERVICE_TYPES = ["ClusterIP", "NodePort", "LoadBalancer"];
const SERVICE_PROTOCOLS = ["TCP", "UDP"];
const ACCESS_MODES = ["ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany"];
const PV_TYPES = ["hostPath", "nfs", "local"];
const RECLAIM_POLICIES = ["Retain", "Delete", "Recycle"];

const SOURCE_LABELS = {
  value: "Plain value",
  existingConfigMap: "Existing ConfigMap",
  createConfigMap: "Create ConfigMap",
  existingSecret: "Existing Secret",
  createSecret: "Create Secret",
};

const VOLUME_KIND_SOURCES = {
  configMap: ["existingConfigMap", "createConfigMap"],
  secret: ["existingSecret", "createSecret"],
};

// ConfigMap/Secret keys must match [-._a-zA-Z0-9]+, so an uploaded filename
// (which may contain spaces, colons, etc.) has to be sanitized first.
function toConfigKey(name) {
  const cleaned = (name || "")
    .trim()
    .replace(/[^-._a-zA-Z0-9]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "");
  return cleaned.slice(0, 253) || "file";
}

// `kubectl apply` copies the whole object into a 256 KB annotation, and a
// ConfigMap caps at ~1 MB total — so a single stored file has to stay small.
// base64 inflates ~33%, so we check the *encoded* length, not the raw size.
const MAX_DATA_BYTES = 200 * 1024;

function base64FromArrayBuffer(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

// Decode an uploaded file as UTF-8 text when it is text, otherwise base64 so the
// raw bytes survive (ConfigMap binaryData / Secret data).
function decodeUpload(buffer) {
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
    if (!text.includes("\u0000")) return { value: text, binary: false };
  } catch {
    /* not valid UTF-8 — fall through to base64 */
  }
  return { value: base64FromArrayBuffer(buffer), binary: true };
}

/** A field forces the deployer's attention only when it's required and has no
 * default. Everything else is auto-resolved (or optional) and collapsed. */
function envNeedsInput(field) {
  return Boolean(field.required && !field.default);
}

/** Sanitize an app name into a DNS-1123 label for default Service/port names. */
function dnsSlug(value, fallback = "app") {
  const cleaned = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || fallback;
}

/** Detect distinct container ports from the template's containers. Kubesight's
 * flat template stores ports as plain numbers (the YAML importer flattens
 * spec.template.spec.containers[*].ports[*].containerPort into this list). */
function detectContainerPorts(template) {
  const ports = [];
  for (const container of template?.containers || []) {
    for (const raw of container?.ports || []) {
      const num = Number(typeof raw === "object" && raw ? raw.containerPort : raw);
      if (Number.isInteger(num) && num >= 1 && num <= 65535 && !ports.includes(num)) {
        ports.push(num);
      }
    }
  }
  return ports;
}

/** Build the initial Service Exposure answer by pre-filling from detected ports.
 * When ports exist we default to creating a ClusterIP Service (the deployer can
 * still opt out); when none are found we default to no Service. */
function initServiceExposure(template) {
  const detected = detectContainerPorts(template);
  const appName = dnsSlug(template?.id || template?.name);
  const templateSvc = template?.networking?.service || {};
  const defaultType = SERVICE_TYPES.includes(templateSvc.type) ? templateSvc.type : "ClusterIP";
  // Source the pre-filled ports from the detected container ports; if the template
  // declares a Service port but no container ports were found, seed that instead.
  let seedPorts = detected;
  if (!seedPorts.length && templateSvc.enabled && Number(templateSvc.port)) {
    seedPorts = [Number(templateSvc.port)];
  }
  return {
    // Default to creating a Service whenever ports exist, or when the template
    // already declares one — but never silently; the deployer can still opt out.
    createService: detected.length > 0 || Boolean(templateSvc.enabled),
    serviceName: String(templateSvc.name || `${appName}-service`),
    serviceType: defaultType,
    ports: seedPorts.map((port) => ({
      include: true,
      name: `tcp-${port}`,
      protocol: "TCP",
      port,
      targetPort: port,
      nodePort: "",
    })),
  };
}

/** Build the initial answers from the template schema's defaults. */
function initAnswers(template, defaultClusterId) {
  const schema = template.schema || {};
  const env = {};
  for (const field of schema.env || []) {
    const sources = field.allowedSources || ["value"];
    env[field.key] = { source: sources[0], value: "", configMapName: "", secretName: "", key: field.key };
  }
  const volumes = {};
  for (const vm of schema.volumeMounts || []) {
    if (!vm.mountPath) continue;
    const sources = vm.allowedSources || VOLUME_KIND_SOURCES[vm.kind] || VOLUME_KIND_SOURCES.configMap;
    // Default the created file's key to the subPath, or the mount path's basename
    // (e.g. /opt/wso2KEY/wso2carbon.jks → wso2carbon.jks), so a "Create" mount lines
    // up with the file the container expects at that path.
    const defaultKey = (vm.subPath || vm.mountPath.split("/").pop() || "").trim();
    volumes[vm.mountPath] = {
      source: sources[0],
      configMapName: "",
      secretName: "",
      items: [{ key: defaultKey, value: "", binary: false }],
    };
  }
  const ips = schema.imagePullSecret;
  const ts = template.storage || {};
  return {
    basics: { appName: template.id || "", namespace: "", clusterId: defaultClusterId || "" },
    overrides: {},
    imagePullSecret: { mode: ips?.mode || "none", name: ips?.name || "", registry: "", username: "", password: "", email: "" },
    env,
    extraEnv: [],
    volumes,
    storage: {
      enabled: Boolean(ts.pvcMode && ts.pvcMode !== "none"),
      mode: ts.pvcMode === "existing" ? "existing" : "new",
      existingPvc: ts.existingPvc || "",
      mountPath: ts.volumeMounts?.[0]?.mountPath || "/data",
      newPvc: {
        name: ts.newPvc?.name || `${template.id || "app"}-data`,
        size: ts.newPvc?.size || "1Gi",
        accessMode: ts.newPvc?.accessMode || "ReadWriteOnce",
        storageClass: ts.newPvc?.storageClass || "",
      },
      pv: {
        enabled: false, name: "", capacity: "", storageType: "hostPath", reclaimPolicy: "Retain",
        hostPath: "", nfsServer: "", nfsPath: "", localPath: "", nodeName: "",
      },
    },
    serviceExposure: initServiceExposure(template),
    ingress: schema.ingress?.supported
      ? { host: "", path: "/", tls: { mode: "none", secret: "", cert: "", key: "" } }
      : null,
    changeSummary: "",
  };
}

/** Deep-merge partial answers (e.g. from an imported deployment form) onto the
 * template-derived defaults so structure (env sub-fields, storage, ...) is kept. */
function mergeAnswers(base, extra) {
  if (!extra || typeof extra !== "object") return base;
  const out = Array.isArray(base) ? [...base] : { ...base };
  for (const [key, value] of Object.entries(extra)) {
    const current = out[key];
    if (
      value && typeof value === "object" && !Array.isArray(value) &&
      current && typeof current === "object" && !Array.isArray(current)
    ) {
      out[key] = mergeAnswers(current, value);
    } else {
      out[key] = value;
    }
  }
  return out;
}

/** Validate the Service Exposure step. Returns an error string ('' when valid) so
 * the wizard can block Next and surface the reason inline. Mirrors the backend
 * resolver's checks so the deployer sees problems before the round-trip. */
function validateServiceStep(svc, { ingressRequired }) {
  if (!svc?.createService) {
    if (ingressRequired) return "Ingress requires a Service. Please create or select a Service.";
    return "";
  }
  if (!String(svc.serviceName || "").trim()) return "Enter a service name.";
  const included = (svc.ports || []).filter((p) => p.include);
  if (!included.length) {
    return "A Service requires at least one port — include a port or disable the Service.";
  }
  const seen = new Set();
  for (const p of included) {
    const port = Number(p.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return `Service port "${p.port ?? ""}" must be a whole number between 1 and 65535.`;
    }
    if (seen.has(port)) return `Service port ${port} is listed more than once.`;
    seen.add(port);
    if (p.targetPort !== "" && p.targetPort != null) {
      const tp = Number(p.targetPort);
      if (!Number.isInteger(tp) || tp < 1 || tp > 65535) {
        return `Target port "${p.targetPort}" must be a whole number between 1 and 65535.`;
      }
    }
    if (p.nodePort !== "" && p.nodePort != null) {
      if (svc.serviceType !== "NodePort") return "A nodePort can only be set when the service type is NodePort.";
      const np = Number(p.nodePort);
      if (!Number.isInteger(np) || np < 30000 || np > 32767) {
        return `nodePort "${p.nodePort}" must be a whole number between 30000 and 32767.`;
      }
    }
  }
  return "";
}

/** Steps are derived from the schema — empty sections are skipped entirely. The
 * Environment step appears whenever the template declares any variable; required
 * ones are shown up front and optional/defaulted ones are collapsed. */
function buildSteps(template) {
  const schema = template.schema || {};
  const steps = [{ key: "basics", label: "Basics" }];
  // Environment is always available so deployers can add ad-hoc variables, even
  // when the template declares none.
  steps.push({ key: "environment", label: "Environment" });
  if ((schema.dependencies || []).length) steps.push({ key: "dependencies", label: "Dependencies" });
  // Storage is available for any pod workload so the deployer can attach a PVC/PV.
  if (POD_KINDS.has(template.workloadType || "Deployment")) steps.push({ key: "storage", label: "Storage" });
  if ((schema.volumeMounts || []).length) steps.push({ key: "volumes", label: "Volumes" });
  // Service Exposure lets the deployer front a selector-addressable workload with a
  // Service. Always offered for those kinds — the step itself handles the
  // "no ports detected" case so a Service can still be created manually.
  if (SERVICE_WORKLOADS.has(template.workloadType || "Deployment")) {
    steps.push({ key: "service", label: "Service Exposure" });
  }
  if (schema.ingress?.supported) steps.push({ key: "ingress", label: "Ingress" });
  steps.push({ key: "review", label: "Review & Deploy" });
  return steps;
}

export default function SchemaDeployWizard({
  open,
  template,
  onClose,
  onSuccess,
  clusterOptions = [],
  defaultClusterId = "",
  initialAnswers = null,
}) {
  const clusterSelectOptions = normalizeClusterOptions(clusterOptions);
  const schema = template?.schema || {};
  const overrides = schema.overrides || {};

  const steps = useMemo(() => (template ? buildSteps(template) : []), [template]);
  const [stepIndex, setStepIndex] = useState(0);
  const [answers, setAnswers] = useState(() =>
    mergeAnswers(initAnswers(template || {}, defaultClusterId), initialAnswers),
  );
  const [resolved, setResolved] = useState(null);
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [configResources, setConfigResources] = useState({ configMaps: [], secrets: [] });
  const [storageClasses, setStorageClasses] = useState([]);
  const [eligibility, setEligibility] = useState(null);
  const [showCloseConfirm, setShowCloseConfirm] = useState(false);

  const clusterId = answers.basics.clusterId;
  const namespace = answers.basics.namespace;

  // HPA at deploy time is guarded by the *merged* resource requests: the
  // template defaults overlaid with any resource override the deployer entered.
  const hpaSupported = ["Deployment", "StatefulSet"].includes(template?.workloadType || "Deployment");
  const mergedHasRequests = useMemo(() => {
    const ov = answers.overrides.resources || {};
    const cpu = String(ov.cpuRequest || template?.resources?.cpuRequest || "").trim();
    const mem = String(ov.memoryRequest || template?.resources?.memoryRequest || "").trim();
    return Boolean(cpu && mem);
  }, [answers.overrides.resources, template]);
  const hpaChecked =
    (answers.overrides.hpaEnabled ?? template?.scaling?.hpa?.enabled ?? false) && mergedHasRequests;
  const templateHpa = template?.scaling?.hpa || {};

  useEffect(() => {
    if (!open || !template) return;
    setStepIndex(0);
    // Merge any imported/prefilled answers over the template defaults so opening
    // the wizard from an imported deployment form keeps the filled values.
    setAnswers(mergeAnswers(initAnswers(template, defaultClusterId), initialAnswers));
    setResolved(null);
    setConfirmation("");
    setError("");
    setShowCloseConfirm(false);
  }, [open, template, defaultClusterId, initialAnswers]);

  // Load the namespace's ConfigMaps/Secrets so "existing" sources offer real
  // names and keys instead of free text.
  useEffect(() => {
    if (!open || !clusterId || !namespace) {
      setConfigResources({ configMaps: [], secrets: [] });
      return undefined;
    }
    let cancelled = false;
    listNamespaceConfigResources(clusterId, namespace)
      .then((res) => {
        if (cancelled) return;
        setConfigResources({
          configMaps: Array.isArray(res?.configMaps) ? res.configMaps : [],
          secrets: Array.isArray(res?.secrets) ? res.secrets : [],
        });
      })
      .catch(() => {
        if (!cancelled) setConfigResources({ configMaps: [], secrets: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [open, clusterId, namespace]);

  // Check whether this cluster requires an approved deployment request before a
  // deploy is allowed, so we can gate the Deploy button up front. The backend 403
  // remains the authoritative enforcement.
  useEffect(() => {
    if (!open || !clusterId) {
      setEligibility(null);
      return undefined;
    }
    let cancelled = false;
    setEligibility(null);
    getClusterDeployEligibility(clusterId)
      .then((res) => {
        if (!cancelled) setEligibility(res || null);
      })
      .catch(() => {
        // Treat an errored check as "unknown" — don't block; the backend gate
        // still enforces approval on the actual deploy.
        if (!cancelled) setEligibility(null);
      });
    return () => {
      cancelled = true;
    };
  }, [open, clusterId]);

  // Storage classes for the chosen cluster (new-PVC picker).
  useEffect(() => {
    if (!open || !clusterId) {
      setStorageClasses([]);
      return undefined;
    }
    let cancelled = false;
    listStorageClasses(clusterId)
      .then((items) => {
        if (!cancelled) setStorageClasses(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setStorageClasses([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open, clusterId]);

  if (!open || !template) return null;

  const step = steps[stepIndex];
  const confirmationPhrase = answers.basics.namespace ? `APPLY ${answers.basics.namespace}` : "";

  // Closing the wizard discards everything the deployer has entered, so any
  // user-initiated dismissal (overlay click, ×, Cancel) asks for confirmation
  // first. Programmatic closes after a successful deploy/add-to-bundle still call
  // onClose directly and skip this.
  const requestClose = () => {
    if (busy) return;
    setShowCloseConfirm(true);
  };
  const confirmClose = () => {
    setShowCloseConfirm(false);
    onClose();
  };
  const cancelClose = () => setShowCloseConfirm(false);

  const setBasics = (key, value) =>
    setAnswers((a) => ({ ...a, basics: { ...a.basics, [key]: value } }));
  const setOverride = (key, value) =>
    setAnswers((a) => ({ ...a, overrides: { ...a.overrides, [key]: value } }));
  const setEnv = (envKey, patch) =>
    setAnswers((a) => ({ ...a, env: { ...a.env, [envKey]: { ...a.env[envKey], ...patch } } }));
  const addExtraEnv = () =>
    setAnswers((a) => ({
      ...a,
      extraEnv: [...a.extraEnv, { name: "", source: "value", value: "", configMapName: "", secretName: "", key: "" }],
    }));
  const updateExtraEnv = (index, patch) =>
    setAnswers((a) => ({ ...a, extraEnv: a.extraEnv.map((row, i) => (i === index ? { ...row, ...patch } : row)) }));
  const removeExtraEnv = (index) =>
    setAnswers((a) => ({ ...a, extraEnv: a.extraEnv.filter((_, i) => i !== index) }));

  // When a deployer picks "Create ConfigMap/Secret", default the resource name from
  // the application name so related vars land in one resource (and stay editable).
  const refDefaultName = (source) => {
    const base = (answers.basics.appName || "app").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "app";
    if (source === "createConfigMap") return `${base}-config`;
    if (source === "createSecret") return `${base}-secret`;
    return "";
  };
  const sourcePatch = (current, source) => {
    const patch = { source };
    if (source === "createConfigMap" && !current.configMapName) patch.configMapName = refDefaultName(source);
    if (source === "createSecret" && !current.secretName) patch.secretName = refDefaultName(source);
    return patch;
  };
  const changeEnvSource = (key, source) => setEnv(key, sourcePatch(answers.env[key] || {}, source));
  const changeExtraSource = (index, source) => updateExtraEnv(index, sourcePatch(answers.extraEnv[index] || {}, source));

  // --- volume mount handlers ---
  const setVolume = (mountPath, patch) =>
    setAnswers((a) => ({ ...a, volumes: { ...a.volumes, [mountPath]: { ...a.volumes[mountPath], ...patch } } }));
  const changeVolumeSource = (mountPath, source) =>
    setVolume(mountPath, sourcePatch(answers.volumes[mountPath] || {}, source));
  const updateVolumeItem = (mountPath, index, patch) =>
    setAnswers((a) => {
      const vol = a.volumes[mountPath] || {};
      const items = (vol.items || []).map((it, i) => (i === index ? { ...it, ...patch } : it));
      return { ...a, volumes: { ...a.volumes, [mountPath]: { ...vol, items } } };
    });
  const addVolumeItem = (mountPath) =>
    setAnswers((a) => {
      const vol = a.volumes[mountPath] || {};
      return { ...a, volumes: { ...a.volumes, [mountPath]: { ...vol, items: [...(vol.items || []), { key: "", value: "", binary: false }] } } };
    });
  const removeVolumeItem = (mountPath, index) =>
    setAnswers((a) => {
      const vol = a.volumes[mountPath] || {};
      const items = (vol.items || []).filter((_, i) => i !== index);
      return { ...a, volumes: { ...a.volumes, [mountPath]: { ...vol, items: items.length ? items : [{ key: "", value: "", binary: false }] } } };
    });
  // Load a file into an item: text files stay readable; binary files are base64'd
  // (kubelet decodes them on mount). The key defaults to a sanitized filename.
  const uploadVolumeFile = (mountPath, index, file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const { value, binary } = decodeUpload(reader.result);
      if (value.length > MAX_DATA_BYTES) {
        setError(
          `"${file.name}" is too large to store in a ConfigMap/Secret (max ~${Math.round(
            MAX_DATA_BYTES / 1024,
          )} KB${binary ? " once base64-encoded" : ""}). Use a volume or object storage for large files.`,
        );
        return;
      }
      setError("");
      setAnswers((a) => {
        const vol = a.volumes[mountPath] || {};
        const items = (vol.items || []).map((it, i) =>
          i === index ? { ...it, value, binary, key: it.key || toConfigKey(file.name) } : it,
        );
        return { ...a, volumes: { ...a.volumes, [mountPath]: { ...vol, items } } };
      });
    };
    reader.readAsArrayBuffer(file);
  };
  const setStorage = (patch) =>
    setAnswers((a) => ({ ...a, storage: { ...a.storage, ...patch } }));
  const setNewPvc = (patch) =>
    setAnswers((a) => ({ ...a, storage: { ...a.storage, newPvc: { ...a.storage.newPvc, ...patch } } }));
  const setPv = (patch) =>
    setAnswers((a) => ({ ...a, storage: { ...a.storage, pv: { ...a.storage.pv, ...patch } } }));
  const setService = (patch) =>
    setAnswers((a) => ({ ...a, serviceExposure: { ...a.serviceExposure, ...patch } }));
  const updateServicePort = (index, patch) =>
    setAnswers((a) => ({
      ...a,
      serviceExposure: {
        ...a.serviceExposure,
        ports: (a.serviceExposure.ports || []).map((p, i) => (i === index ? { ...p, ...patch } : p)),
      },
    }));
  const addServicePort = () =>
    setAnswers((a) => ({
      ...a,
      serviceExposure: {
        ...a.serviceExposure,
        ports: [
          ...(a.serviceExposure.ports || []),
          { include: true, name: "", protocol: "TCP", port: "", targetPort: "", nodePort: "" },
        ],
      },
    }));
  const removeServicePort = (index) =>
    setAnswers((a) => ({
      ...a,
      serviceExposure: {
        ...a.serviceExposure,
        ports: (a.serviceExposure.ports || []).filter((_, i) => i !== index),
      },
    }));
  const setIngress = (patch) =>
    setAnswers((a) => ({ ...a, ingress: { ...a.ingress, ...patch } }));
  const setTls = (patch) =>
    setAnswers((a) => ({ ...a, ingress: { ...a.ingress, tls: { ...a.ingress.tls, ...patch } } }));
  const setIps = (patch) =>
    setAnswers((a) => ({ ...a, imagePullSecret: { ...a.imagePullSecret, ...patch } }));

  const resolve = async () => {
    const result = await resolveWizardTemplate(template.id, answers);
    setResolved(result);
    return result;
  };

  const basicsValid = answers.basics.appName.trim() && answers.basics.namespace.trim() && answers.basics.clusterId;

  const goNext = async () => {
    setError("");
    const nextIndex = stepIndex + 1;
    if (steps[nextIndex]?.key === "review") {
      setBusy(true);
      try {
        await resolve();
      } catch (err) {
        setError(err.message || "Could not assemble the deployment.");
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    setStepIndex(nextIndex);
  };

  const goBack = () => {
    setError("");
    if (stepIndex > 0) setStepIndex((i) => i - 1);
  };

  const deploy = async () => {
    setBusy(true);
    setError("");
    try {
      const payload = resolved?.payload || (await resolve()).payload;
      const result = await applyWizardDeploy({ ...payload, confirmation });
      onSuccess?.(result);
      onClose();
    } catch (err) {
      setError(err.message || "Deploy failed");
    } finally {
      setBusy(false);
    }
  };

  // An Ingress routes through a Service, so whenever this template offers an Ingress
  // step the deployer must keep a Service. Mirrors the backend resolver's rule.
  const ingressStepPresent = steps.some((s) => s.key === "ingress");
  const ingressNeedsService = ingressStepPresent && !answers.serviceExposure?.createService;
  const serviceStepError =
    step.key === "service"
      ? validateServiceStep(answers.serviceExposure, { ingressRequired: ingressNeedsService })
      : "";

  const canProceed =
    step.key === "basics" ? basicsValid : step.key === "service" ? !serviceStepError : true;

  // Only block when the eligibility check is definitive: approval required and the
  // user has no active approval. In-flight or errored checks never disable Deploy —
  // the backend 403 is the authoritative fallback surfaced in `error`.
  const approvalBlocked = Boolean(
    eligibility && eligibility.approvalRequired && !eligibility.hasActiveApproval,
  );
  const requiredApprovals = eligibility?.requiredApprovals;

  return (
    <div className="modal-overlay wizard-overlay" role="presentation" onClick={requestClose}>
      <section
        className="card modal-panel wizard-modal"
        role="dialog"
        aria-labelledby="schema-wizard-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="wizard-modal__header">
          <div>
            <h3 id="schema-wizard-title">Deploy {template.name}</h3>
            <p className="muted">{template.description || "Fill in only what this template leaves open."}</p>
          </div>
          <button type="button" className="modal-close" onClick={requestClose} aria-label="Close">×</button>
        </header>

        <nav className="wizard-stepper" aria-label="Deployment steps">
          {steps.map((s, index) => (
            <button
              key={s.key}
              type="button"
              className={`wizard-stepper__item${index === stepIndex ? " is-active" : ""}${index < stepIndex ? " is-complete" : ""}`}
              onClick={() => index < stepIndex && setStepIndex(index)}
              disabled={index > stepIndex}
            >
              <span className="wizard-stepper__number">{index + 1}</span>
              <span className="wizard-stepper__label">{s.label}</span>
            </button>
          ))}
        </nav>

        <div className="wizard-modal__body">
          {error ? <p className="error-banner">{error}</p> : null}

          {step.key === "basics" ? (
            <div className="wizard-step-panel">
              <h4>Basics</h4>
              {approvalBlocked ? (
                <p
                  className="error-banner"
                  style={{
                    background: "#fee2e2",
                    border: "1px solid #dc2626",
                    color: "#b91c1c",
                    fontWeight: 600,
                    padding: "0.75rem 1rem",
                    borderRadius: "8px",
                  }}
                >
                  This cluster requires an approved deployment request — request one from
                  the Clusters tab
                  {requiredApprovals
                    ? ` (needs ${requiredApprovals} approval${requiredApprovals === 1 ? "" : "s"})`
                    : ""}
                  .
                </p>
              ) : null}
              <Field label="Application name">
                <input value={answers.basics.appName} onChange={(e) => setBasics("appName", e.target.value)} placeholder="orders" />
              </Field>
              <Field label="Cluster">
                <SearchableSelect value={answers.basics.clusterId} onChange={(e) => setBasics("clusterId", e.target.value)}>
                  <option value="">— select cluster —</option>
                  {clusterSelectOptions.map((c) => (
                    <option key={c.id} value={c.id}>{c.name || c.id}</option>
                  ))}
                </SearchableSelect>
              </Field>
              <Field label="Namespace">
                <NamespaceSelect
                  clusterId={clusterId}
                  value={answers.basics.namespace}
                  onChange={(e) => setBasics("namespace", e.target.value)}
                />
              </Field>

              {overrides.tag ? (
                <Field label="Image tag">
                  <input value={answers.overrides.tag || ""} onChange={(e) => setOverride("tag", e.target.value)} placeholder={template.containers?.[0]?.tag || "latest"} />
                </Field>
              ) : null}
              {overrides.replicas ? (
                <Field label="Replicas">
                  <input
                    type="number"
                    min="0"
                    value={answers.overrides.replicas ?? ""}
                    onChange={(e) => setOverride("replicas", e.target.value)}
                    placeholder={String(template.scaling?.replicas ?? 1)}
                  />
                </Field>
              ) : null}
              {overrides.storageSize ? (
                <Field label="Storage size">
                  <input value={answers.overrides.storageSize || ""} onChange={(e) => setOverride("storageSize", e.target.value)} placeholder={template.storage?.newPvc?.size || "1Gi"} />
                </Field>
              ) : null}
              {overrides.image ? (
                <Field label="Image">
                  <input value={answers.overrides.image || ""} onChange={(e) => setOverride("image", e.target.value)} placeholder={template.containers?.[0]?.image || ""} />
                </Field>
              ) : null}
              {overrides.resources ? (
                <div className="schema-override-grid">
                  {["cpuRequest", "cpuLimit", "memoryRequest", "memoryLimit"].map((rk) => (
                    <Field key={rk} label={rk}>
                      <input
                        value={answers.overrides.resources?.[rk] || ""}
                        onChange={(e) => setOverride("resources", { ...answers.overrides.resources, [rk]: e.target.value })}
                        placeholder={template.resources?.[rk] || ""}
                      />
                    </Field>
                  ))}
                </div>
              ) : null}

              {hpaSupported ? (
                <div className="schema-hpa-block">
                  <label className={`wizard-checkbox${mergedHasRequests ? "" : " wizard-checkbox--disabled"}`}>
                    <input
                      type="checkbox"
                      checked={hpaChecked}
                      disabled={!mergedHasRequests}
                      onChange={(e) => setOverride("hpaEnabled", e.target.checked)}
                    />
                    Enable Horizontal Pod Autoscaler
                  </label>
                  {!mergedHasRequests ? (
                    <p className="wizard-hpa-warning">
                      ⚠️ HPA requires CPU and Memory requests. Define them on the template or via the
                      resource overrides above.
                    </p>
                  ) : null}
                  {hpaChecked ? (
                    <>
                      <div className="schema-override-grid">
                        {[
                          { key: "minReplicas", label: "Min replicas", fallback: 1 },
                          { key: "maxReplicas", label: "Max replicas", fallback: 5 },
                          { key: "cpuThreshold", label: "CPU threshold %", fallback: "" },
                          { key: "memoryThreshold", label: "Memory threshold %", fallback: "" },
                        ].map(({ key, label, fallback }) => (
                          <Field key={key} label={label}>
                            <input
                              type="number"
                              value={answers.overrides.hpa?.[key] ?? ""}
                              onChange={(e) =>
                                setOverride("hpa", { ...answers.overrides.hpa, [key]: e.target.value })
                              }
                              placeholder={String(templateHpa[key] ?? fallback) || "optional"}
                            />
                          </Field>
                        ))}
                      </div>
                      <p className="muted" style={{ margin: "0.25rem 0 0" }}>
                        Leave a field blank to keep the template default. Scale on CPU, memory, or both.
                      </p>
                    </>
                  ) : null}
                </div>
              ) : null}

              {(() => {
                const ipsSchema = schema.imagePullSecret;
                const locked = Boolean(ipsSchema?.mode && !ipsSchema.overridable);
                return (
                <div className="schema-ips-block">
                  <span className="wizard-field__label">Image pull secret</span>
                  {locked ? (
                    <p className="muted" style={{ margin: 0 }}>Mode: {answers.imagePullSecret?.mode}</p>
                  ) : (
                    <SearchableSelect value={answers.imagePullSecret?.mode || "none"} onChange={(e) => setIps({ mode: e.target.value })}>
                      <option value="none">None</option>
                      <option value="existing">Existing secret</option>
                      <option value="create">Create secret</option>
                    </SearchableSelect>
                  )}
                  {answers.imagePullSecret?.mode === "existing" ? (
                    (() => {
                      // Image pull secrets must be docker-registry secrets; only those
                      // are selectable here (other secret types can't authenticate a pull).
                      const pickable = configResources.secrets.filter(
                        (s) => s.type === "kubernetes.io/dockerconfigjson" || s.type === "kubernetes.io/dockercfg",
                      );
                      return (
                        <Field label="Secret name">
                          <SearchableSelect
                            value={answers.imagePullSecret?.name || ""}
                            onChange={(e) => setIps({ name: e.target.value })}
                            disabled={!pickable.length}
                          >
                            <option value="">
                              {pickable.length ? "— select secret —" : "no docker-registry secrets"}
                            </option>
                            {pickable.map((s) => (
                              <option key={s.name} value={s.name}>{s.name}</option>
                            ))}
                          </SearchableSelect>
                        </Field>
                      );
                    })()
                  ) : null}
                  {answers.imagePullSecret?.mode === "create" ? (
                    <>
                      <Field label="Registry">
                        <input value={answers.imagePullSecret?.registry || ""} onChange={(e) => setIps({ registry: e.target.value })} placeholder="https://index.docker.io/v1/" />
                      </Field>
                      <Field label="Username">
                        <input value={answers.imagePullSecret?.username || ""} onChange={(e) => setIps({ username: e.target.value })} placeholder="robot$pull" />
                      </Field>
                      <Field label="Password / token">
                        <input type="password" value={answers.imagePullSecret?.password || ""} onChange={(e) => setIps({ password: e.target.value })} />
                      </Field>
                    </>
                  ) : null}
                </div>
                );
              })()}
            </div>
          ) : null}

          {step.key === "environment" ? (
            <div className="wizard-step-panel">
              <h4>Environment variables</h4>
              {(() => {
                const fields = schema.env || [];
                const required = fields.filter(envNeedsInput);
                const optional = fields.filter((f) => !envNeedsInput(f));
                const renderField = (field) => {
                  const ans = answers.env[field.key] || {};
                  const allowed = field.allowedSources || ["value"];
                  const source = ans.source || allowed[0];
                  return (
                    <div key={field.key} className="schema-env-card">
                      <div className="schema-env-card__top">
                        <strong>{field.key}</strong>
                        {field.required ? <span className="schema-pill schema-pill--req">required</span> : <span className="schema-pill">optional</span>}
                        {field.sensitive ? <span className="schema-pill schema-pill--sensitive">sensitive</span> : null}
                      </div>
                      {field.description ? <p className="muted" style={{ margin: 0 }}>{field.description}</p> : null}
                      {/* Single allowed source → no dropdown, just the input. */}
                      {allowed.length > 1 ? (
                        <SearchableSelect value={source} onChange={(e) => changeEnvSource(field.key, e.target.value)} aria-label={`${field.key} source`}>
                          {allowed.map((s) => (
                            <option key={s} value={s}>{SOURCE_LABELS[s] || s}</option>
                          ))}
                        </SearchableSelect>
                      ) : (
                        <p className="muted" style={{ margin: 0 }}>Source: {SOURCE_LABELS[source] || source}</p>
                      )}
                      <EnvSourceInputs
                        field={field}
                        source={source}
                        ans={ans}
                        setEnv={(patch) => setEnv(field.key, patch)}
                        configMaps={configResources.configMaps}
                        secrets={configResources.secrets}
                      />
                    </div>
                  );
                };
                return (
                  <>
                    {required.map(renderField)}
                    {/* When nothing is strictly required, show the optional vars
                        directly so they're never hidden; otherwise collapse them. */}
                    {optional.length && required.length ? (
                      <details className="schema-optional-env">
                        <summary>{optional.length} optional variable{optional.length > 1 ? "s" : ""} (using defaults)</summary>
                        <div className="schema-env-list" style={{ marginTop: "var(--space-3)" }}>
                          {optional.map(renderField)}
                        </div>
                      </details>
                    ) : (
                      optional.map(renderField)
                    )}
                  </>
                );
              })()}

              <div className="schema-extra-env">
                <h5>Additional variables</h5>
                <p className="muted" style={{ marginTop: 0 }}>
                  Add variables beyond the template — each with any source.
                </p>
                {answers.extraEnv.length ? (
                  <div className="schema-env-list">
                    {answers.extraEnv.map((row, index) => {
                      const pseudoField = { key: row.name || `variable ${index + 1}`, sensitive: false };
                      return (
                        <div key={index} className="schema-env-card">
                          <div className="schema-env-card__top">
                            <input
                              value={row.name}
                              onChange={(e) => updateExtraEnv(index, { name: e.target.value })}
                              placeholder="VAR_NAME"
                              aria-label={`Custom variable ${index + 1} name`}
                              className="schema-env-card__key"
                            />
                            <SearchableSelect
                              value={row.source}
                              onChange={(e) => changeExtraSource(index, e.target.value)}
                              aria-label={`Custom variable ${index + 1} source`}
                            >
                              {Object.entries(SOURCE_LABELS).map(([value, label]) => (
                                <option key={value} value={value}>{label}</option>
                              ))}
                            </SearchableSelect>
                            <button
                              type="button"
                              className="btn-outline template-env-row__remove"
                              onClick={() => removeExtraEnv(index)}
                              aria-label={`Remove custom variable ${index + 1}`}
                            >
                              ×
                            </button>
                          </div>
                          <EnvSourceInputs
                            field={pseudoField}
                            source={row.source}
                            ans={row}
                            setEnv={(patch) => updateExtraEnv(index, patch)}
                            configMaps={configResources.configMaps}
                            secrets={configResources.secrets}
                          />
                        </div>
                      );
                    })}
                  </div>
                ) : null}
                <button type="button" className="btn-outline" onClick={addExtraEnv}>
                  + Add variable
                </button>
              </div>
            </div>
          ) : null}

          {step.key === "dependencies" ? (
            <div className="wizard-step-panel">
              <h4>Dependencies</h4>
              <p className="muted" style={{ marginTop: 0 }}>
                This application expects the following backing services to be available. Provision them separately.
              </p>
              {(schema.dependencies || []).map((dep) => (
                <div key={dep.name} className="schema-dep-card">
                  <div className="schema-dep-card__top">
                    <strong>{dep.name}</strong>
                    <span className="schema-pill">{dep.kind}</span>
                    {dep.required ? (
                      <span className="schema-pill schema-pill--req">required</span>
                    ) : (
                      <span className="schema-pill">optional</span>
                    )}
                  </div>
                  {dep.note ? <p className="muted" style={{ margin: 0 }}>{dep.note}</p> : null}
                </div>
              ))}
            </div>
          ) : null}

          {step.key === "storage" ? (
            <div className="wizard-step-panel">
              <h4>Storage</h4>
              <label className="wizard-checkbox">
                <input type="checkbox" checked={answers.storage.enabled} onChange={(e) => setStorage({ enabled: e.target.checked })} />
                Attach persistent storage (PVC)
              </label>
              {answers.storage.enabled ? (
                <>
                  <Field label="PVC">
                    <SearchableSelect value={answers.storage.mode} onChange={(e) => setStorage({ mode: e.target.value })}>
                      <option value="new">Create new PVC</option>
                      <option value="existing">Use existing PVC</option>
                    </SearchableSelect>
                  </Field>
                  <Field label="Mount path">
                    <input value={answers.storage.mountPath} onChange={(e) => setStorage({ mountPath: e.target.value })} placeholder="/data" />
                  </Field>

                  {answers.storage.mode === "existing" ? (
                    <Field label="Existing PVC name">
                      <input value={answers.storage.existingPvc} onChange={(e) => setStorage({ existingPvc: e.target.value })} placeholder="shared-data" />
                    </Field>
                  ) : (
                    <>
                      <div className="schema-override-grid">
                        <Field label="PVC name">
                          <input value={answers.storage.newPvc.name} onChange={(e) => setNewPvc({ name: e.target.value })} placeholder="data" />
                        </Field>
                        <Field label="Size">
                          <input value={answers.storage.newPvc.size} onChange={(e) => setNewPvc({ size: e.target.value })} placeholder="1Gi" />
                        </Field>
                        <Field label="Access mode">
                          <SearchableSelect value={answers.storage.newPvc.accessMode} onChange={(e) => setNewPvc({ accessMode: e.target.value })}>
                            {ACCESS_MODES.map((m) => (
                              <option key={m} value={m}>{m}</option>
                            ))}
                          </SearchableSelect>
                        </Field>
                        <Field label="Storage class">
                          {storageClasses.length ? (
                            <SearchableSelect
                              value={answers.storage.newPvc.storageClass}
                              onChange={(e) => setNewPvc({ storageClass: e.target.value })}
                              disabled={answers.storage.pv.enabled}
                            >
                              <option value="">(cluster default)</option>
                              {storageClasses.map((sc) => (
                                <option key={sc.name} value={sc.name}>{sc.default ? `${sc.name} (default)` : sc.name}</option>
                              ))}
                            </SearchableSelect>
                          ) : (
                            <input
                              value={answers.storage.newPvc.storageClass}
                              onChange={(e) => setNewPvc({ storageClass: e.target.value })}
                              placeholder="(cluster default)"
                              disabled={answers.storage.pv.enabled}
                            />
                          )}
                        </Field>
                      </div>

                      <details className="schema-optional-env">
                        <summary>Advanced: manually create a PersistentVolume</summary>
                        <label className="wizard-checkbox" style={{ marginTop: "var(--space-3)" }}>
                          <input type="checkbox" checked={answers.storage.pv.enabled} onChange={(e) => setPv({ enabled: e.target.checked })} />
                          Create a PersistentVolume and bind it to this PVC
                        </label>
                        {answers.storage.pv.enabled ? (
                          <div className="schema-override-grid" style={{ marginTop: "var(--space-3)" }}>
                            <Field label="PV name">
                              <input value={answers.storage.pv.name} onChange={(e) => setPv({ name: e.target.value })} placeholder="(auto from PVC)" />
                            </Field>
                            <Field label="Capacity">
                              <input value={answers.storage.pv.capacity} onChange={(e) => setPv({ capacity: e.target.value })} placeholder={answers.storage.newPvc.size} />
                            </Field>
                            <Field label="Volume type">
                              <SearchableSelect value={answers.storage.pv.storageType} onChange={(e) => setPv({ storageType: e.target.value })}>
                                {PV_TYPES.map((t) => (
                                  <option key={t} value={t}>{t}</option>
                                ))}
                              </SearchableSelect>
                            </Field>
                            <Field label="Reclaim policy">
                              <SearchableSelect value={answers.storage.pv.reclaimPolicy} onChange={(e) => setPv({ reclaimPolicy: e.target.value })}>
                                {RECLAIM_POLICIES.map((p) => (
                                  <option key={p} value={p}>{p}</option>
                                ))}
                              </SearchableSelect>
                            </Field>
                            {answers.storage.pv.storageType === "hostPath" ? (
                              <Field label="Host path">
                                <input value={answers.storage.pv.hostPath} onChange={(e) => setPv({ hostPath: e.target.value })} placeholder="/data" />
                              </Field>
                            ) : null}
                            {answers.storage.pv.storageType === "nfs" ? (
                              <>
                                <Field label="NFS server">
                                  <input value={answers.storage.pv.nfsServer} onChange={(e) => setPv({ nfsServer: e.target.value })} placeholder="10.0.0.10" />
                                </Field>
                                <Field label="NFS path">
                                  <input value={answers.storage.pv.nfsPath} onChange={(e) => setPv({ nfsPath: e.target.value })} placeholder="/exports/data" />
                                </Field>
                              </>
                            ) : null}
                            {answers.storage.pv.storageType === "local" ? (
                              <>
                                <Field label="Local path">
                                  <input value={answers.storage.pv.localPath} onChange={(e) => setPv({ localPath: e.target.value })} placeholder="/mnt/data" />
                                </Field>
                                <Field label="Node name">
                                  <input value={answers.storage.pv.nodeName} onChange={(e) => setPv({ nodeName: e.target.value })} placeholder="worker-1" />
                                </Field>
                              </>
                            ) : null}
                          </div>
                        ) : null}
                      </details>
                    </>
                  )}
                </>
              ) : null}
            </div>
          ) : null}

          {step.key === "volumes" ? (
            <div className="wizard-step-panel">
              <h4>Volumes</h4>
              <p className="muted" style={{ marginTop: 0 }}>
                Each mount sources a ConfigMap or Secret as files. Pick an existing resource or create one.
              </p>
              {(schema.volumeMounts || []).map((vm) => {
                if (!vm.mountPath) return null;
                const ans = answers.volumes[vm.mountPath] || {};
                const kind = vm.kind === "secret" ? "secret" : "configMap";
                const allowed = vm.allowedSources || VOLUME_KIND_SOURCES[kind];
                const source = ans.source || allowed[0];
                const isSecret = kind === "secret";
                const isCreate = source === "createConfigMap" || source === "createSecret";
                const resources = isSecret ? configResources.secrets : configResources.configMaps;
                const nameKey = isSecret ? "secretName" : "configMapName";
                return (
                  <div key={vm.mountPath} className="schema-env-card">
                    <div className="schema-env-card__top">
                      <strong>{vm.mountPath}</strong>
                      <span className="schema-pill">{isSecret ? "Secret" : "ConfigMap"}</span>
                      {vm.readOnly ? <span className="schema-pill">read-only</span> : null}
                    </div>
                    <SearchableSelect
                      value={source}
                      onChange={(e) => changeVolumeSource(vm.mountPath, e.target.value)}
                      aria-label={`${vm.mountPath} source`}
                    >
                      {allowed.map((s) => (
                        <option key={s} value={s}>{SOURCE_LABELS[s] || s}</option>
                      ))}
                    </SearchableSelect>
                    {!isCreate ? (
                      resources.length ? (
                        <SearchableSelect
                          value={ans[nameKey] || ""}
                          onChange={(e) => setVolume(vm.mountPath, { [nameKey]: e.target.value })}
                          aria-label={`${vm.mountPath} ${isSecret ? "secret" : "configmap"} name`}
                        >
                          <option value="">— select {isSecret ? "secret" : "configmap"} —</option>
                          {resources.map((r) => (
                            <option key={r.name} value={r.name}>{r.name}</option>
                          ))}
                        </SearchableSelect>
                      ) : (
                        <>
                          <p className="schema-ref-empty muted">
                            No existing {isSecret ? "secret" : "configmap"}s in this namespace — switch the source to “Create” to add one.
                          </p>
                          <SearchableSelect disabled aria-label={`${vm.mountPath} ${isSecret ? "secret" : "configmap"} name`}>
                            <option value="">no existing {isSecret ? "secret" : "configmap"}s found</option>
                          </SearchableSelect>
                        </>
                      )
                    ) : (
                      <div className="schema-volume-create">
                        <input
                          value={ans[nameKey] || ""}
                          onChange={(e) => setVolume(vm.mountPath, { [nameKey]: e.target.value })}
                          placeholder={`new ${isSecret ? "secret" : "configmap"} name`}
                        />
                        {(ans.items || []).map((item, index) => (
                          <div key={index} className="schema-volume-file">
                            <div className="schema-volume-file__row">
                              <input
                                value={item.key || ""}
                                onChange={(e) => updateVolumeItem(vm.mountPath, index, { key: e.target.value })}
                                placeholder="file name (key)"
                              />
                              <label className="btn-outline schema-upload-btn">
                                Upload
                                <input
                                  type="file"
                                  className="schema-upload-btn__input"
                                  onChange={(e) => {
                                    uploadVolumeFile(vm.mountPath, index, e.target.files?.[0]);
                                    e.target.value = "";
                                  }}
                                />
                              </label>
                              <label className="schema-volume-file__binary checkbox-row">
                                <input
                                  type="checkbox"
                                  checked={Boolean(item.binary)}
                                  onChange={(e) => updateVolumeItem(vm.mountPath, index, { binary: e.target.checked })}
                                />
                                <span>Base64</span>
                              </label>
                              <button
                                type="button"
                                className="btn-outline template-env-row__remove"
                                onClick={() => removeVolumeItem(vm.mountPath, index)}
                                aria-label={`Remove file ${index + 1}`}
                              >
                                ×
                              </button>
                            </div>
                            <textarea
                              className="schema-volume-file__content"
                              rows={6}
                              value={item.value || ""}
                              onChange={(e) => updateVolumeItem(vm.mountPath, index, { value: e.target.value })}
                              placeholder={
                                item.binary
                                  ? "base64-encoded binary — uploaded files fill this automatically"
                                  : "file content — type, paste, or upload"
                              }
                            />
                          </div>
                        ))}
                        <button type="button" className="btn-outline" onClick={() => addVolumeItem(vm.mountPath)}>
                          + Add file
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : null}

          {step.key === "service" ? (
            <div className="wizard-step-panel">
              <h4>Service Exposure</h4>
              {(() => {
                const svc = answers.serviceExposure || {};
                const detected = detectContainerPorts(template);
                const selectorLabel = `app: ${dnsSlug(answers.basics.appName || template.id)}`;
                return (
                  <>
                    {detected.length ? (
                      <p className="muted" style={{ marginTop: 0 }}>
                        This deployment exposes container port{detected.length > 1 ? "s" : ""}{" "}
                        ({detected.join(", ")}). Do you want to create a Kubernetes Service?
                      </p>
                    ) : (
                      <p className="muted" style={{ marginTop: 0 }}>
                        No container ports were found. You can still create a Service manually if needed.
                      </p>
                    )}

                    <label className="wizard-checkbox">
                      <input
                        type="checkbox"
                        checked={Boolean(svc.createService)}
                        onChange={(e) => setService({ createService: e.target.checked })}
                      />
                      Create a Kubernetes Service
                    </label>

                    {ingressNeedsService ? (
                      <p className="error-banner" role="alert" style={{ marginTop: "var(--space-3)" }}>
                        Ingress requires a Service. Please create or select a Service.
                      </p>
                    ) : null}

                    {svc.createService ? (
                      <>
                        <div className="schema-override-grid">
                          <Field label="Service name">
                            <input
                              value={svc.serviceName || ""}
                              onChange={(e) => setService({ serviceName: e.target.value })}
                              placeholder={`${dnsSlug(answers.basics.appName || template.id)}-service`}
                            />
                          </Field>
                          <Field label="Service type">
                            <SearchableSelect
                              value={svc.serviceType || "ClusterIP"}
                              onChange={(e) => setService({ serviceType: e.target.value })}
                            >
                              {SERVICE_TYPES.map((t) => (
                                <option key={t} value={t}>{t}</option>
                              ))}
                            </SearchableSelect>
                          </Field>
                        </div>

                        <p className="muted" style={{ margin: "var(--space-2) 0 0" }}>
                          Selector is locked to the deployment&apos;s pod labels ({selectorLabel}) to avoid
                          breaking routing.
                        </p>

                        <h5 style={{ marginBottom: 0 }}>Ports</h5>
                        {(svc.ports || []).length ? (
                          (svc.ports || []).map((p, index) => (
                            <div key={index} className="schema-env-card">
                              <div className="schema-env-card__top">
                                <label className="checkbox-row" style={{ marginRight: "auto" }}>
                                  <input
                                    type="checkbox"
                                    checked={Boolean(p.include)}
                                    onChange={(e) => updateServicePort(index, { include: e.target.checked })}
                                  />
                                  <span>Include</span>
                                </label>
                                <button
                                  type="button"
                                  className="btn-outline template-env-row__remove"
                                  onClick={() => removeServicePort(index)}
                                  aria-label={`Remove port ${index + 1}`}
                                >
                                  ×
                                </button>
                              </div>
                              <div className="schema-override-grid">
                                <Field label="Name">
                                  <input
                                    value={p.name || ""}
                                    onChange={(e) => updateServicePort(index, { name: e.target.value })}
                                    placeholder={`${(p.protocol || "tcp").toLowerCase()}-${p.port || "port"}`}
                                    disabled={!p.include}
                                  />
                                </Field>
                                <Field label="Protocol">
                                  <SearchableSelect
                                    value={p.protocol || "TCP"}
                                    onChange={(e) => updateServicePort(index, { protocol: e.target.value })}
                                    disabled={!p.include}
                                  >
                                    {SERVICE_PROTOCOLS.map((proto) => (
                                      <option key={proto} value={proto}>{proto}</option>
                                    ))}
                                  </SearchableSelect>
                                </Field>
                                <Field label="Port">
                                  <input
                                    type="number"
                                    min="1"
                                    max="65535"
                                    value={p.port ?? ""}
                                    onChange={(e) => updateServicePort(index, { port: e.target.value })}
                                    disabled={!p.include}
                                  />
                                </Field>
                                <Field label="Target port">
                                  <input
                                    type="number"
                                    min="1"
                                    max="65535"
                                    value={p.targetPort ?? ""}
                                    onChange={(e) => updateServicePort(index, { targetPort: e.target.value })}
                                    disabled={!p.include}
                                  />
                                </Field>
                                {svc.serviceType === "NodePort" ? (
                                  <Field label="Node port (30000–32767)">
                                    <input
                                      type="number"
                                      min="30000"
                                      max="32767"
                                      value={p.nodePort ?? ""}
                                      onChange={(e) => updateServicePort(index, { nodePort: e.target.value })}
                                      placeholder="auto"
                                      disabled={!p.include}
                                    />
                                  </Field>
                                ) : null}
                              </div>
                            </div>
                          ))
                        ) : (
                          <p className="muted" style={{ marginTop: 0 }}>No ports yet — add one below.</p>
                        )}
                        <button type="button" className="btn-outline" onClick={addServicePort}>
                          + Add port
                        </button>
                        {serviceStepError ? (
                          <p className="error-banner" role="alert" style={{ marginTop: "var(--space-3)" }}>
                            {serviceStepError}
                          </p>
                        ) : null}
                      </>
                    ) : null}
                  </>
                );
              })()}
            </div>
          ) : null}

          {step.key === "ingress" ? (
            <div className="wizard-step-panel">
              <h4>Ingress</h4>
              <Field label="Host">
                <input value={answers.ingress?.host || ""} onChange={(e) => setIngress({ host: e.target.value })} placeholder="orders.example.com" />
              </Field>
              <Field label="Path">
                <input value={answers.ingress?.path || ""} onChange={(e) => setIngress({ path: e.target.value })} placeholder="/" />
              </Field>
              {schema.ingress?.tls ? (
                <>
                  <Field label="TLS">
                    <SearchableSelect value={answers.ingress?.tls?.mode || "none"} onChange={(e) => setTls({ mode: e.target.value })}>
                      <option value="none">No TLS</option>
                      <option value="existing">Existing certificate secret</option>
                      <option value="create">Create new certificate secret</option>
                    </SearchableSelect>
                  </Field>
                  {answers.ingress?.tls?.mode === "existing" ? (
                    (() => {
                      const tlsSecrets = configResources.secrets.filter((s) => s.type === "kubernetes.io/tls");
                      return (
                        <Field label="TLS secret">
                          {tlsSecrets.length ? (
                            <SearchableSelect value={answers.ingress?.tls?.secret || ""} onChange={(e) => setTls({ secret: e.target.value })}>
                              <option value="">— select TLS secret —</option>
                              {tlsSecrets.map((s) => (
                                <option key={s.name} value={s.name}>{s.name}</option>
                              ))}
                            </SearchableSelect>
                          ) : (
                            <input value={answers.ingress?.tls?.secret || ""} onChange={(e) => setTls({ secret: e.target.value })} placeholder="no TLS secrets — type a name" />
                          )}
                        </Field>
                      );
                    })()
                  ) : null}
                  {answers.ingress?.tls?.mode === "create" ? (
                    <>
                      <Field label="New secret name">
                        <input value={answers.ingress?.tls?.secret || ""} onChange={(e) => setTls({ secret: e.target.value })} placeholder="orders-tls" />
                      </Field>
                      <Field label="Certificate (tls.crt)">
                        <textarea rows={4} value={answers.ingress?.tls?.cert || ""} onChange={(e) => setTls({ cert: e.target.value })} placeholder="-----BEGIN CERTIFICATE-----" />
                      </Field>
                      <Field label="Private key (tls.key)">
                        <textarea rows={4} value={answers.ingress?.tls?.key || ""} onChange={(e) => setTls({ key: e.target.value })} placeholder="-----BEGIN PRIVATE KEY-----" />
                      </Field>
                    </>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}

          {step.key === "review" ? (
            <div className="wizard-step-panel">
              <h4>Review &amp; Deploy</h4>
              {approvalBlocked ? (
                <p
                  className="error-banner"
                  style={{
                    background: "#fee2e2",
                    border: "1px solid #dc2626",
                    color: "#b91c1c",
                    fontWeight: 600,
                    padding: "0.75rem 1rem",
                    borderRadius: "8px",
                  }}
                >
                  This cluster requires an approved deployment request — request one from
                  the Clusters tab
                  {requiredApprovals
                    ? ` (needs ${requiredApprovals} approval${requiredApprovals === 1 ? "" : "s"})`
                    : ""}
                  .
                </p>
              ) : null}
              {resolved?.summary ? (
                <ul className="schema-review-summary">
                  <li><span>Application</span><strong>{resolved.summary.appName}</strong></li>
                  <li><span>Namespace</span><strong>{resolved.summary.namespace}</strong></li>
                  <li><span>Workload</span><strong>{resolved.summary.workloadType}</strong></li>
                  <li><span>Resources</span><strong>{(resolved.summary.resources || []).map((r) => r.kind).join(", ")}</strong></li>
                </ul>
              ) : null}
              {(resolved?.summary?.warnings || []).length ? (
                <div className="wizard-hpa-warning" role="status">
                  {resolved.summary.warnings.map((warning) => (
                    <p key={warning} style={{ margin: 0 }}>⚠️ {warning}</p>
                  ))}
                </div>
              ) : null}
              <Field label="Change summary">
                <input value={answers.changeSummary} onChange={(e) => setAnswers((a) => ({ ...a, changeSummary: e.target.value }))} placeholder="Initial deploy" />
              </Field>
              <details className="schema-yaml-disclosure">
                <summary>View generated manifests</summary>
                <YamlPreviewPanel yaml={resolved?.yaml || ""} readOnly />
              </details>
              <Field label={`Confirmation — type "${confirmationPhrase}"`}>
                <input value={confirmation} onChange={(e) => setConfirmation(e.target.value)} placeholder={confirmationPhrase} />
              </Field>
            </div>
          ) : null}
        </div>

        <footer className="wizard-modal__footer modal-actions">
          <button type="button" className="btn-outline" onClick={stepIndex === 0 ? requestClose : goBack} disabled={busy}>
            {stepIndex === 0 ? "Cancel" : "Back"}
          </button>
          {step.key === "review" ? (
            <>
              <AddToBundleButton
                className="btn-outline"
                label="Add to Bundle"
                disabled={busy || !resolved?.yaml}
                descriptor={{
                  actionType: "create_from_template",
                  clusterId,
                  namespace,
                  resourceKind: resolved?.summary?.workloadType || "Deployment",
                  resourceName: resolved?.summary?.appName || answers.basics.appName,
                  yaml: resolved?.yaml,
                }}
                onAdded={onClose}
              />
              <button type="button" className="btn-primary" onClick={deploy} disabled={busy || confirmation !== confirmationPhrase || approvalBlocked}>
                {busy ? "Deploying…" : "Deploy Application"}
              </button>
            </>
          ) : (
            <button type="button" className="btn-primary" onClick={goNext} disabled={busy || !canProceed}>
              {busy ? "Working…" : "Next"}
            </button>
          )}
        </footer>
      </section>

      {showCloseConfirm ? (
        <div
          className="modal-overlay wizard-close-confirm"
          role="presentation"
          onClick={(e) => {
            e.stopPropagation();
            cancelClose();
          }}
        >
          <section
            className="card modal-panel wizard-close-confirm__panel"
            role="alertdialog"
            aria-labelledby="wizard-close-confirm-title"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="modal-header">
              <h3 id="wizard-close-confirm-title">Discard this deployment?</h3>
            </header>
            <p className="muted" style={{ margin: "var(--space-2) 0 var(--space-4)" }}>
              You have unsaved changes in this wizard. Closing it now will discard
              everything you&apos;ve entered.
            </p>
            <footer className="modal-actions">
              <button type="button" className="btn-outline" onClick={cancelClose}>
                Keep editing
              </button>
              <button type="button" className="btn-primary" onClick={confirmClose}>
                Discard &amp; close
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}

/** Name + key picker for an "existing ConfigMap/Secret" source. Renders dropdowns
 * populated from the namespace's resources, falling back to free text when the
 * list is empty (e.g. namespace not yet selected or RBAC blocks the listing). */
function ExistingRefPicker({ resources, nameKey, ans, setEnv, nameLabel }) {
  const name = ans[nameKey] || "";
  const selected = resources.find((r) => r.name === name);
  const keys = selected?.keys || [];
  // Choosing a resource pre-selects its first key when the current one is invalid.
  const onNameChange = (value) => {
    const match = resources.find((r) => r.name === value);
    const matchKeys = match?.keys || [];
    const nextKey = matchKeys.includes(ans.key) ? ans.key : matchKeys[0] || "";
    setEnv({ [nameKey]: value, key: nextKey });
  };
  const kindWord = nameLabel.replace(/\s*name$/i, "") || "resource";
  const hasSelection = Boolean(selected);
  return (
    <>
      {resources.length ? null : (
        <p className="schema-ref-empty muted">No existing {kindWord}s in this namespace — switch the source to “Create” to add one.</p>
      )}
      <div className="schema-ref-inputs">
        {resources.length ? (
          <SearchableSelect value={name} onChange={(e) => onNameChange(e.target.value)} aria-label={nameLabel}>
            <option value="">— select {nameLabel} —</option>
            {resources.map((r) => (
              <option key={r.name} value={r.name}>{r.name}</option>
            ))}
          </SearchableSelect>
        ) : (
          <SearchableSelect disabled aria-label={nameLabel}>
            <option value="">no existing {kindWord}s found</option>
          </SearchableSelect>
        )}
        {keys.length ? (
          <SearchableSelect value={ans.key || ""} onChange={(e) => setEnv({ key: e.target.value })} aria-label="key">
            <option value="">— select key —</option>
            {keys.map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </SearchableSelect>
        ) : (
          <input
            value={ans.key || ""}
            onChange={(e) => setEnv({ key: e.target.value })}
            placeholder="key"
            disabled={!hasSelection}
          />
        )}
      </div>
    </>
  );
}

function EnvSourceInputs({ field, source, ans, setEnv, configMaps = [], secrets = [] }) {
  const valueType = field.sensitive ? "password" : "text";
  if (source === "value") {
    return (
      <input
        type={valueType}
        value={ans.value || ""}
        onChange={(e) => setEnv({ value: e.target.value })}
        placeholder="value"
        aria-label={`${field.key} value`}
      />
    );
  }
  if (source === "existingConfigMap") {
    return <ExistingRefPicker resources={configMaps} nameKey="configMapName" ans={ans} setEnv={setEnv} nameLabel="configmap name" />;
  }
  if (source === "createConfigMap") {
    return (
      <div className="schema-ref-inputs">
        <input value={ans.configMapName || ""} onChange={(e) => setEnv({ configMapName: e.target.value })} placeholder="configmap name" />
        <input value={ans.key || ""} onChange={(e) => setEnv({ key: e.target.value })} placeholder="key" />
        <input value={ans.value || ""} onChange={(e) => setEnv({ value: e.target.value })} placeholder="value" />
      </div>
    );
  }
  if (source === "existingSecret") {
    return <ExistingRefPicker resources={secrets} nameKey="secretName" ans={ans} setEnv={setEnv} nameLabel="secret name" />;
  }
  // createSecret
  return (
    <div className="schema-ref-inputs">
      <input value={ans.secretName || ""} onChange={(e) => setEnv({ secretName: e.target.value })} placeholder="secret name" />
      <input value={ans.key || ""} onChange={(e) => setEnv({ key: e.target.value })} placeholder="key" />
      <input type="password" value={ans.value || ""} onChange={(e) => setEnv({ value: e.target.value })} placeholder="value" />
    </div>
  );
}

function Field({ label, children }) {
  return (
    <label className="wizard-field">
      <span className="wizard-field__label">{label}</span>
      {children}
    </label>
  );
}
