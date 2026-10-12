// The Create/Edit Template form's model: the form state, how a stored template
// fills it, how the form turns back into a template payload, and — the part that
// matters on edit — how the form's edits are applied onto the stored template.
//
// The form shows only part of what a template can hold: the first container,
// one container port, a ClusterIP service, a size + path per persistent volume.
// Imported or API-made templates can carry much more (sidecar containers, a
// manual PersistentVolume per claim, NodePort / multi-port services, ingress,
// probe timeouts, keys this form has never heard of). `mergeTemplateEdit`
// starts from the stored template and writes back only what the user changed in
// the form, so everything the form can't show survives a save verbatim.
//
// Pure functions only — no React — so they are unit-tested in
// templateFormModel.test.js.

export const NEW_CATEGORY = "__new__";

export const KIND_SOURCES = {
  value: ["value"],
  configMap: ["existingConfigMap", "createConfigMap"],
  secret: ["existingSecret", "createSecret"],
};

export const VOLUME_KIND_SOURCES = {
  configMap: ["existingConfigMap", "createConfigMap"],
  secret: ["existingSecret", "createSecret"],
};

export const PROBE_KEYS = [
  { key: "readiness", label: "Readiness probe" },
  { key: "liveness", label: "Liveness probe" },
  { key: "startup", label: "Startup probe" },
];

// A row in the environment-variable *schema*: requirements a deployer fills later,
// not a concrete value. `default` pre-fills the deployer's field for plain-value vars.
export const EMPTY_ENV = { key: "", required: false, sensitive: false, kind: "value", default: "" };

// Dependencies are informational only — they document the backing services an
// app needs; they don't wire anything automatically.
export const EMPTY_DEPENDENCY = { kind: "postgresql", name: "", required: true, note: "" };

// A volume-mount schema row: the deployer supplies the actual ConfigMap/Secret
// (existing or freshly created) at deploy time.
export const EMPTY_VOLUME = { mountPath: "", kind: "configMap", readOnly: false, subPath: "" };

// A persistent-storage volume: each row becomes its own PVC + mount.
export const EMPTY_STORAGE_VOLUME = { size: "1Gi", mountPath: "" };

// Every override the deployer may be granted lives here — the single home for the
// lock-vs-override decision.
export const DEFAULT_OVERRIDES = {
  image: false,
  tag: true,
  replicas: true,
  resources: false,
  storageSize: false,
  ingressHost: false,
};

export const PROBE_DEFAULTS = {
  readiness: { enabled: false, type: "http", path: "/", port: "", command: "", initialDelaySeconds: "5", periodSeconds: "10" },
  liveness: { enabled: false, type: "http", path: "/", port: "", command: "", initialDelaySeconds: "15", periodSeconds: "20" },
  startup: { enabled: false, type: "http", path: "/", port: "", command: "", initialDelaySeconds: "10", periodSeconds: "10" },
};

export function freshProbes() {
  return {
    readiness: { ...PROBE_DEFAULTS.readiness },
    liveness: { ...PROBE_DEFAULTS.liveness },
    startup: { ...PROBE_DEFAULTS.startup },
  };
}

export const EMPTY_FORM = {
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
  // Autoscaling (HPA) — requires CPU + memory requests to be defined
  hpaEnabled: false,
  hpaMinReplicas: "1",
  hpaMaxReplicas: "5",
  hpaCpuThreshold: "80",
  hpaMemoryThreshold: "",
  // Deployment schema
  envVars: [{ ...EMPTY_ENV }],
  overrides: { ...DEFAULT_OVERRIDES },
  dependencies: [],
  volumeMounts: [],
  ingressTls: false,
  // Storage (simplified — advanced PVC/PV details are not authored here).
  // Each volume row becomes its own PVC + mount at deploy time.
  storageEnabled: false,
  storageVolumes: [{ size: "1Gi", mountPath: "/data" }],
  probes: freshProbes(),
};

export function freshForm(category) {
  return {
    ...EMPTY_FORM,
    category,
    envVars: [{ ...EMPTY_ENV }],
    overrides: { ...DEFAULT_OVERRIDES },
    dependencies: [],
    volumeMounts: [],
    storageVolumes: [{ size: "1Gi", mountPath: "/data" }],
    probes: freshProbes(),
  };
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isIndex = (value) => Number.isInteger(value) && value >= 0;
const asList = (value) => (Array.isArray(value) ? value : []);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));

// The claims a stored storage block declares: multi-volume templates carry a
// `newPvcs` list (one PVC per mount, linked by pvcName); legacy templates have a
// single `newPvc` shared by every mount.
function storedPvcList(storage) {
  if (asList(storage?.newPvcs).length) return storage.newPvcs;
  return isObject(storage?.newPvc) ? [storage.newPvc] : [];
}

// Rebuild the editable form from a saved template detail (or an import draft).
// List rows remember where they came from (`_origin`: their index in the stored
// list) so an edit can be written back onto the row it was read from.
export function formFromTemplate(detail) {
  const container = detail.containers?.[0] || {};
  const res = detail.resources || {};
  const hpa = detail.scaling?.hpa || {};
  const schema = detail.schema || {};
  const overrides = schema.overrides || {};
  const storage = detail.storage || {};
  const service = detail.networking?.service;

  const envVars = asList(schema.env).map((f, i) => ({
    key: f.key || "",
    required: Boolean(f.required),
    sensitive: Boolean(f.sensitive),
    kind: f.sensitive ? "secret" : f.kind || "value",
    default: f.default || "",
    _origin: i,
  }));
  const dependencies = asList(schema.dependencies).map((d, i) => ({
    kind: d.kind || "postgresql",
    name: d.name || "",
    required: Boolean(d.required),
    note: d.note || "",
    _origin: i,
  }));
  const volumeMounts = asList(schema.volumeMounts).map((v, i) => ({
    mountPath: v.mountPath || "",
    kind: v.kind === "secret" ? "secret" : "configMap",
    readOnly: Boolean(v.readOnly),
    subPath: v.subPath || "",
    _origin: i,
  }));

  // One editable row per persistent mount, showing the size of its claim.
  const storagePvcs = storedPvcList(storage);
  const storageVolumes = asList(storage.volumeMounts).map((vm, i) => {
    let pvcIndex = vm.pvcName ? storagePvcs.findIndex((p) => p.name === vm.pvcName) : -1;
    if (pvcIndex < 0) pvcIndex = storagePvcs[i] ? i : storagePvcs.length ? 0 : -1;
    const pvc = storagePvcs[pvcIndex] || {};
    return {
      size: pvc.size || "1Gi",
      mountPath: vm.mountPath || "",
      _origin: i,
      _pvc: pvcIndex >= 0 ? pvcIndex : null,
    };
  });

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
    hpaEnabled: Boolean(hpa.enabled),
    hpaMinReplicas: hpa.minReplicas != null ? String(hpa.minReplicas) : "1",
    hpaMaxReplicas: hpa.maxReplicas != null ? String(hpa.maxReplicas) : "5",
    hpaCpuThreshold: hpa.cpuThreshold != null ? String(hpa.cpuThreshold) : "80",
    hpaMemoryThreshold: hpa.memoryThreshold != null ? String(hpa.memoryThreshold) : "",
    envVars: envVars.length ? envVars : [{ ...EMPTY_ENV }],
    overrides: {
      image: Boolean(overrides.image),
      tag: Boolean(overrides.tag),
      replicas: Boolean(overrides.replicas),
      resources: Boolean(overrides.resources),
      storageSize: Boolean(overrides.storageSize),
      ingressHost: Boolean(schema.ingress?.supported),
    },
    dependencies,
    volumeMounts,
    ingressTls: Boolean(schema.ingress?.tls),
    storageEnabled: Boolean(storage.pvcMode && storage.pvcMode !== "none"),
    storageVolumes: storageVolumes.length ? storageVolumes : [{ size: storage.newPvc?.size || "1Gi", mountPath: "/data" }],
    probes,
  };
}

export const slugify = (value) =>
  String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

// HPA can only be enabled when both CPU and memory requests are defined,
// since autoscaling computes utilization relative to those requests.
export const hasHpaResources = (form) => Boolean(form.cpuRequest.trim() && form.memoryRequest.trim());

export const resolveCategory = (form) =>
  (form.category === NEW_CATEGORY ? form.newCategory : form.category).trim();

// The first problem that blocks saving, or "" when the form can be saved.
export function validateTemplateForm(form) {
  if (!form.name.trim()) return "Template name is required.";
  if (!resolveCategory(form)) return "Choose an existing category or enter a new one.";
  if (!form.image.trim()) return "Container image is required.";
  if (
    form.hpaEnabled &&
    hasHpaResources(form) &&
    !form.hpaCpuThreshold.trim() &&
    !form.hpaMemoryThreshold.trim()
  ) {
    return "Set a CPU threshold, a memory threshold, or both for the autoscaler.";
  }
  return "";
}

function buildProbe(probe, fallbackPort) {
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
}

// Environment-variable *schema*: each row declares a kind + requirements, not
// a value. The deployer chooses existing-vs-create for ConfigMap/Secret kinds.
function buildEnvField(row) {
  const key = (row.key || "").trim();
  if (!key) return null;
  const sensitive = Boolean(row.sensitive);
  const kind = sensitive ? "secret" : row.kind || "value";
  const field = { key, required: Boolean(row.required), sensitive, kind, allowedSources: KIND_SOURCES[kind] };
  if (kind === "value" && (row.default || "").trim()) field.default = row.default.trim();
  return field;
}

function buildDependency(dep) {
  const name = (dep.name || dep.kind || "").trim();
  if (!name) return null;
  return {
    kind: dep.kind,
    name,
    required: Boolean(dep.required),
    ...(dep.note?.trim() ? { note: dep.note.trim() } : {}),
  };
}

// Volume mounts: each declares a ConfigMap/Secret-backed mount path. The
// deployer chooses existing-vs-create for the backing resource at deploy time.
function buildVolumeMount(row) {
  const mountPath = (row.mountPath || "").trim();
  if (!mountPath) return null;
  const kind = row.kind === "secret" ? "secret" : "configMap";
  const subPath = (row.subPath || "").trim();
  return {
    mountPath,
    kind,
    allowedSources: VOLUME_KIND_SOURCES[kind],
    readOnly: Boolean(row.readOnly),
    ...(subPath ? { subPath } : {}),
  };
}

const storageRow = (row) => ({ size: (row.size || "").trim() || "1Gi", mountPath: (row.mountPath || "").trim() });

// The template payload the form describes on its own — everything a brand-new
// template is made of. (On edit, `mergeTemplateEdit` applies it onto the stored
// template instead of sending it as is.)
export function buildTemplatePayload(form) {
  const name = form.name.trim();
  const port = Number.parseInt(form.port, 10);
  const replicas = Number.parseInt(form.replicas, 10);
  const containerName = slugify(name) || "main";
  const hpaReady = hasHpaResources(form);
  const payload = {
    name,
    description: form.description.trim(),
    category: resolveCategory(form),
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
    scaling: {
      replicas: Number.isFinite(replicas) ? replicas : 1,
      // HPA needs CPU + memory requests to compute utilization. Force it off
      // when either is missing so we never persist an unusable autoscaler.
      hpa: {
        enabled: form.hpaEnabled && hpaReady,
        minReplicas: Number.parseInt(form.hpaMinReplicas, 10) || 1,
        maxReplicas: Number.parseInt(form.hpaMaxReplicas, 10) || 5,
        // CPU and memory thresholds are independent — set either, or both.
        cpuThreshold: form.hpaCpuThreshold.trim() ? Number.parseInt(form.hpaCpuThreshold, 10) : null,
        memoryThreshold: form.hpaMemoryThreshold.trim() ? Number.parseInt(form.hpaMemoryThreshold, 10) : null,
      },
    },
  };
  if (form.serviceEnabled && Number.isFinite(port)) {
    payload.networking = {
      service: { enabled: true, type: "ClusterIP", port, targetPort: port, protocol: "TCP" },
    };
  }

  const envSchema = form.envVars.map(buildEnvField).filter(Boolean);
  // Carry non-sensitive defaults through as legacy prefills so summaries stay populated.
  const envVars = envSchema.filter((f) => f.default).map((f) => ({ name: f.key, value: f.default }));
  if (envVars.length) payload.environment = { envVars };

  // Storage (simplified): one PVC + mount per volume row, no advanced PV
  // authoring. Mounts link to their PVC via pvcName; `newPvc` mirrors the
  // first PVC for older consumers of the single-volume shape.
  if (form.storageEnabled) {
    const rows = form.storageVolumes.map(storageRow).filter((v) => v.mountPath);
    const volumes = rows.length ? rows : [{ size: "1Gi", mountPath: "/data" }];
    const newPvcs = volumes.map((v, i) => ({
      name: i === 0 ? `${containerName}-data` : `${containerName}-data-${i + 1}`,
      size: v.size,
      accessMode: "ReadWriteOnce",
    }));
    payload.storage = {
      pvcMode: "new",
      newPvc: { ...newPvcs[0] },
      newPvcs,
      volumeMounts: volumes.map((v, i) => ({
        name: i === 0 ? "data" : `data-${i + 1}`,
        mountPath: v.mountPath,
        readOnly: false,
        pvcName: newPvcs[i].name,
      })),
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

  // Deployment schema: overrides, env requirements, dependencies, ingress.
  const schema = {};
  const overrides = {};
  for (const key of ["image", "tag", "replicas", "resources", "storageSize"]) {
    if (form.overrides[key]) overrides[key] = true;
  }
  if (Object.keys(overrides).length) schema.overrides = overrides;
  if (envSchema.length) schema.env = envSchema;
  const dependencies = form.dependencies.map(buildDependency).filter(Boolean);
  if (dependencies.length) schema.dependencies = dependencies;
  const volumeMounts = form.volumeMounts.map(buildVolumeMount).filter(Boolean);
  if (volumeMounts.length) schema.volumeMounts = volumeMounts;
  if (form.overrides.ingressHost) {
    schema.ingress = { supported: true, tls: Boolean(form.ingressTls) };
  }
  if (Object.keys(schema).length) payload.schema = schema;

  return payload;
}

// ---------------------------------------------------------------------------
// Edit round-trip
// ---------------------------------------------------------------------------

// Keys of a template *detail* response (or an import draft) that describe the
// stored row rather than the template body; never sent back on save.
const READ_ONLY_KEYS = ["id", "custom", "createdBy", "ciServices", "warnings"];

// Write onto `target` only the keys whose value differs between what the form
// built before (`prev`) and now (`next`). A key the form no longer emits is
// removed; every key the form never emitted is left exactly as stored.
function applyChanged(target, next = {}, prev = {}) {
  for (const key of new Set([...Object.keys(next), ...Object.keys(prev)])) {
    if (same(next[key], prev[key])) continue;
    if (next[key] === undefined) delete target[key];
    else target[key] = clone(next[key]);
  }
  return target;
}

// Merge an edited list of form rows back onto the stored list. Rows read from
// the stored list (`_origin`) keep every stored key the form doesn't model and
// take only the fields the user changed; new rows are added as the form built
// them; rows the user removed are dropped; stored rows the form never showed
// are kept.
function mergeRows(storedList, rows, initialRows, buildRow) {
  const stored = asList(storedList);
  const shown = new Set();
  const before = new Map();
  for (const row of initialRows) {
    if (!isIndex(row._origin)) continue;
    shown.add(row._origin);
    before.set(row._origin, buildRow(row));
  }
  const merged = [];
  const used = new Set();
  for (const row of rows) {
    const now = buildRow(row);
    if (!now) continue;
    const origin = row._origin;
    if (isIndex(origin) && isObject(stored[origin]) && !used.has(origin)) {
      used.add(origin);
      merged.push(applyChanged(clone(stored[origin]), now, before.get(origin) || {}));
    } else {
      merged.push(now);
    }
  }
  stored.forEach((row, index) => {
    if (!shown.has(index)) merged.push(clone(row));
  });
  return merged;
}

function setOrDelete(target, key, value) {
  const empty =
    value === undefined ||
    (Array.isArray(value) && !value.length) ||
    (isObject(value) && !Object.keys(value).length);
  if (empty) delete target[key];
  else target[key] = value;
}

// The Service follows the container port the form edits: a port or targetPort
// that pointed at the old container port moves with it; any other port (a
// NodePort's service port 80 in front of container port 8080, extra ports) stays.
function moveServicePort(service, oldPort, newPort) {
  const next = { ...service };
  const matches = (value) => value == null || String(value) === String(oldPort);
  if (matches(next.targetPort)) next.targetPort = newPort;
  if (next.port == null || String(next.port) === String(oldPort)) next.port = newPort;
  if (Array.isArray(next.ports)) {
    next.ports = next.ports.map((entry) => {
      if (!isObject(entry) || String(entry.targetPort ?? entry.port) !== String(oldPort)) return entry;
      return {
        ...entry,
        targetPort: newPort,
        ...(String(entry.port) === String(oldPort) ? { port: newPort } : {}),
      };
    });
  }
  return next;
}

function mergeStorage(out, stored, form, initialForm, built, before) {
  const nowStorage = built.storage;
  const beforeStorage = before.storage;
  if (same(nowStorage, beforeStorage)) return;
  if (!nowStorage) {
    delete out.storage; // the user switched persistent storage off
    return;
  }
  const storedStorage = isObject(stored.storage) ? stored.storage : {};
  if (!beforeStorage) {
    // Switched on: the form's volumes, plus whatever else the stored block had.
    out.storage = { ...clone(storedStorage), ...clone(nowStorage) };
    return;
  }

  const storage = clone(storedStorage);
  const storedMounts = asList(storedStorage.volumeMounts);
  const pvcs = clone(storedPvcList(storedStorage));
  const containerName = slugify(built.name) || "main";
  const initialByOrigin = new Map(
    initialForm.storageVolumes.filter((row) => isIndex(row._origin)).map((row) => [row._origin, row]),
  );
  const shownPvcs = new Set(
    initialForm.storageVolumes.filter((row) => isIndex(row._pvc)).map((row) => row._pvc),
  );
  const usedPvcs = new Set();
  const mounts = [];
  const unique = (base, taken) => {
    let candidate = base;
    for (let n = 2; taken.has(candidate); n += 1) candidate = `${base}-${n}`;
    return candidate;
  };

  for (const row of form.storageVolumes) {
    const now = storageRow(row);
    if (!now.mountPath) continue;
    if (isIndex(row._origin) && isObject(storedMounts[row._origin])) {
      const mount = clone(storedMounts[row._origin]);
      const prev = storageRow(initialByOrigin.get(row._origin) || {});
      if (now.mountPath !== prev.mountPath) mount.mountPath = now.mountPath;
      if (isIndex(row._pvc) && pvcs[row._pvc]) {
        if (now.size !== prev.size) pvcs[row._pvc].size = now.size;
        usedPvcs.add(row._pvc);
      }
      mounts.push(mount);
    } else {
      // A volume added in the form: its own claim, named so it can't collide.
      const pvcName = unique(`${containerName}-data`, new Set(pvcs.map((p) => p.name)));
      const mountName = unique("data", new Set([...storedMounts, ...mounts].map((m) => m?.name)));
      pvcs.push({ name: pvcName, size: now.size, accessMode: "ReadWriteOnce" });
      usedPvcs.add(pvcs.length - 1);
      mounts.push({ name: mountName, mountPath: now.mountPath, readOnly: false, pvcName });
    }
  }
  // Mounts the form never showed stay, and so do their claims.
  storedMounts.forEach((mount, index) => {
    if (!initialByOrigin.has(index)) mounts.push(clone(mount));
  });
  if (!mounts.length) {
    out.storage = { ...storage, ...clone(nowStorage) };
    return;
  }
  // A claim survives when a remaining row uses it, or the form never showed it.
  const keptPvcs = pvcs.filter((_, index) => usedPvcs.has(index) || !shownPvcs.has(index));
  storage.volumeMounts = mounts;
  if (Array.isArray(storedStorage.newPvcs) || keptPvcs.length > 1 || (keptPvcs.length && !isObject(storedStorage.newPvc))) {
    storage.newPvcs = keptPvcs;
  }
  if (keptPvcs.length) storage.newPvc = { ...keptPvcs[0] };
  else delete storage.newPvc;
  if (!storage.pvcMode || storage.pvcMode === "none") storage.pvcMode = "new";
  out.storage = storage;
}

/**
 * The payload to save for `form`, given the template it was opened from.
 *
 * `stored` is the template detail (edit) or the import draft (create from
 * YAML); `initialForm` is the form exactly as `formFromTemplate(stored)` filled
 * it. With no `stored`, the form is the whole template. Otherwise the stored
 * template is copied and only what differs between `initialForm` and `form` is
 * written onto it — every container, port, claim, probe field and key the form
 * doesn't show is kept as it was.
 */
export function mergeTemplateEdit(stored, initialForm, form) {
  const built = buildTemplatePayload(form);
  if (!isObject(stored) || !initialForm) return built;
  const before = buildTemplatePayload(initialForm);
  const out = clone(stored);
  for (const key of READ_ONLY_KEYS) delete out[key];
  const changed = (key) => !same(form[key], initialForm[key]);

  // Details are always the form's.
  out.name = built.name;
  out.description = built.description;
  out.category = built.category;
  out.workloadType = built.workloadType;

  // Containers: the form edits the first one; the rest are untouched.
  const containers = asList(stored.containers);
  if (!isObject(containers[0])) {
    out.containers = [built.containers[0], ...clone(containers.slice(1))];
  } else {
    const first = clone(containers[0]);
    const builtFirst = built.containers[0];
    if (changed("image")) first.image = builtFirst.image;
    if (changed("tag")) first.tag = builtFirst.tag;
    if (changed("pullPolicy")) first.pullPolicy = builtFirst.pullPolicy;
    if (changed("port")) first.ports = [...builtFirst.ports, ...asList(first.ports).slice(1)];
    if (!first.name) first.name = builtFirst.name;
    out.containers = [first, ...clone(containers.slice(1))];
  }

  // Resources and scaling: field by field.
  if (!same(built.resources, before.resources)) {
    out.resources = applyChanged(isObject(out.resources) ? out.resources : {}, built.resources, before.resources);
  }
  if (!same(built.scaling, before.scaling)) {
    const scaling = isObject(out.scaling) ? out.scaling : {};
    if (built.scaling.replicas !== before.scaling.replicas) scaling.replicas = built.scaling.replicas;
    if (!same(built.scaling.hpa, before.scaling.hpa)) {
      scaling.hpa = applyChanged(isObject(scaling.hpa) ? scaling.hpa : {}, built.scaling.hpa, before.scaling.hpa);
    }
    out.scaling = scaling;
  }

  // Networking: the form models "expose a Service on the container port".
  // Its type, extra ports and any ingress block are the stored template's.
  const networking = isObject(out.networking) ? out.networking : {};
  const storedService = isObject(networking.service) ? networking.service : null;
  const nowService = built.networking?.service;
  const beforeService = before.networking?.service;
  if (initialForm.serviceEnabled && !form.serviceEnabled) {
    delete networking.service;
  } else if (form.serviceEnabled && !initialForm.serviceEnabled && nowService) {
    networking.service = { ...nowService, ...(storedService || {}), enabled: true };
  } else if (storedService && nowService && beforeService && nowService.port !== beforeService.port) {
    networking.service = moveServicePort(storedService, beforeService.port, nowService.port);
  } else if (!storedService && nowService && !same(nowService, beforeService)) {
    networking.service = nowService;
  }
  setOrDelete(out, "networking", networking);

  // Environment-variable schema and the legacy default prefills derived from it.
  const schema = isObject(out.schema) ? out.schema : {};
  if (changed("envVars")) {
    setOrDelete(schema, "env", mergeRows(stored.schema?.env, form.envVars, initialForm.envVars, buildEnvField));
    if (!same(built.environment, before.environment)) {
      const environment = isObject(out.environment) ? out.environment : {};
      const nowDefaults = asList(built.environment?.envVars);
      const formNames = new Set([...asList(before.environment?.envVars), ...nowDefaults].map((e) => e.name));
      const kept = asList(environment.envVars).filter((entry) => !formNames.has(entry?.name));
      setOrDelete(environment, "envVars", [...kept, ...nowDefaults]);
      setOrDelete(out, "environment", environment);
    }
  }
  if (changed("dependencies")) {
    setOrDelete(
      schema,
      "dependencies",
      mergeRows(stored.schema?.dependencies, form.dependencies, initialForm.dependencies, buildDependency),
    );
  }
  if (changed("volumeMounts")) {
    setOrDelete(
      schema,
      "volumeMounts",
      mergeRows(stored.schema?.volumeMounts, form.volumeMounts, initialForm.volumeMounts, buildVolumeMount),
    );
  }
  const nowOverrides = built.schema?.overrides || {};
  const beforeOverrides = before.schema?.overrides || {};
  if (!same(nowOverrides, beforeOverrides)) {
    setOrDelete(schema, "overrides", applyChanged(isObject(schema.overrides) ? schema.overrides : {}, nowOverrides, beforeOverrides));
  }
  const nowIngress = built.schema?.ingress;
  const beforeIngress = before.schema?.ingress;
  if (!same(nowIngress, beforeIngress)) {
    if (!nowIngress) delete schema.ingress;
    else schema.ingress = applyChanged(isObject(schema.ingress) ? schema.ingress : {}, nowIngress, beforeIngress || {});
  }
  setOrDelete(out, "schema", schema);

  mergeStorage(out, stored, form, initialForm, built, before);

  // Probes: field by field, so timeouts, thresholds and headers survive.
  const healthChecks = isObject(out.healthChecks) ? out.healthChecks : {};
  for (const { key } of PROBE_KEYS) {
    const now = built.healthChecks?.[key];
    const prev = before.healthChecks?.[key];
    if (same(now, prev)) continue;
    if (!now) delete healthChecks[key];
    else healthChecks[key] = applyChanged(isObject(healthChecks[key]) ? healthChecks[key] : {}, now, prev || {});
  }
  setOrDelete(out, "healthChecks", healthChecks);

  return out;
}

// ---------------------------------------------------------------------------
// What the form can't show
// ---------------------------------------------------------------------------

const KNOWN_TOP_LEVEL = new Set([
  ...READ_ONLY_KEYS,
  "name",
  "description",
  "category",
  "workloadType",
  "containers",
  "resources",
  "scaling",
  "networking",
  "storage",
  "healthChecks",
  "environment",
  "schema",
]);
const FORM_CONTAINER_KEYS = new Set(["name", "image", "tag", "pullPolicy", "ports"]);
const FORM_RESOURCE_KEYS = new Set(["cpuRequest", "cpuLimit", "memoryRequest", "memoryLimit"]);
const FORM_PROBE_KEYS =new Set(["enabled", "type", "path", "port", "command", "initialDelaySeconds", "periodSeconds"]);
const FORM_SCHEMA_KEYS = new Set(["overrides", "env", "dependencies", "volumeMounts", "ingress"]);

const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;

/**
 * Plain-language list of what `stored` holds that the form doesn't edit —
 * shown as a notice so nobody thinks a sidecar or a manual PV vanished.
 * Empty when the form shows everything.
 */
export function describeKeptExtras(stored) {
  if (!isObject(stored)) return [];
  const extras = [];
  const containers = asList(stored.containers);
  if (containers.length > 1) {
    const names = containers.slice(1).map((c) => c?.name).filter(Boolean);
    extras.push(
      `${plural(containers.length - 1, "more container")}${names.length ? ` (${names.join(", ")})` : ""}`,
    );
  }
  const first = isObject(containers[0]) ? containers[0] : {};
  if (asList(first.ports).length > 1) extras.push(plural(first.ports.length - 1, "more container port"));
  const containerExtras = Object.keys(first).filter((key) => !FORM_CONTAINER_KEYS.has(key));
  if (containerExtras.length) extras.push(`the container's ${containerExtras.join(", ")}`);
  const resourceExtras = Object.keys(stored.resources || {}).filter((key) => !FORM_RESOURCE_KEYS.has(key));
  if (resourceExtras.length) extras.push(`resource settings (${resourceExtras.join(", ")})`);

  const service = stored.networking?.service;
  if (isObject(service)) {
    const parts = [];
    if (service.type && service.type !== "ClusterIP") parts.push(`type ${service.type}`);
    if (asList(service.ports).length > 1) parts.push(plural(service.ports.length, "port"));
    const containerPort = asList(first.ports)[0];
    const servicePort = service.port ?? asList(service.ports)[0]?.port;
    if (servicePort != null && containerPort != null && String(servicePort) !== String(containerPort)) {
      parts.push(`port ${servicePort}`);
    }
    if (service.enabled === false) parts.push("switched off");
    if (parts.length) extras.push(`Service settings (${parts.join(", ")})`);
  }
  const networkExtras = Object.keys(stored.networking || {}).filter((key) => key !== "service");
  if (networkExtras.length) extras.push(`${networkExtras.join(", ")} networking`);

  const storage = isObject(stored.storage) ? stored.storage : {};
  const pvcs = storedPvcList(storage);
  const manualPv = pvcs.filter((p, i) => p?.advanced?.createManualPv || (i === 0 && storage.advanced?.createManualPv));
  if (manualPv.length) extras.push(`manual PersistentVolume settings for ${plural(manualPv.length, "volume")}`);
  const claimExtras = pvcs.some(
    (p) => p?.storageClass || (p?.accessMode && p.accessMode !== "ReadWriteOnce"),
  );
  if (claimExtras) extras.push("storage class / access mode settings");
  if (storage.pvcMode === "existing" || storage.existingPvc) extras.push("an existing volume claim");

  const probeExtras = Object.values(stored.healthChecks || {}).some(
    (hc) => isObject(hc) && Object.keys(hc).some((key) => !FORM_PROBE_KEYS.has(key)),
  );
  if (probeExtras) extras.push("advanced probe settings");

  const environment = isObject(stored.environment) ? stored.environment : {};
  const envExtras = Object.keys(environment).filter((key) => key !== "envVars");
  if (envExtras.length) extras.push(`environment ${envExtras.join(", ")}`);

  const schemaExtras = Object.keys(stored.schema || {}).filter((key) => !FORM_SCHEMA_KEYS.has(key));
  if (schemaExtras.length) extras.push(`schema ${schemaExtras.join(", ")}`);

  const unknown = Object.keys(stored).filter((key) => !KNOWN_TOP_LEVEL.has(key));
  if (unknown.length) extras.push(`other settings (${unknown.join(", ")})`);
  return extras;
}

// "a, b and c"
export function joinExtras(extras) {
  if (extras.length <= 1) return extras.join("");
  return `${extras.slice(0, -1).join(", ")} and ${extras[extras.length - 1]}`;
}
