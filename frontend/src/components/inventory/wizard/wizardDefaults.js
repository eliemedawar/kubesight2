export const WIZARD_STEPS = [
  { key: "basics", label: "Basics", number: 1 },
  { key: "containers", label: "Containers", number: 2 },
  { key: "environment", label: "Environment", number: 3 },
  { key: "resources", label: "Resources", number: 4 },
  { key: "storage", label: "Storage", number: 5 },
  { key: "networking", label: "Networking", number: 6 },
  { key: "health", label: "Health", number: 7 },
  { key: "scaling", label: "Scaling", number: 8 },
  { key: "validation", label: "Validation", number: 9 },
  { key: "review", label: "Review & Deploy", number: 10 },
];

export const WORKLOAD_TYPES = [
  "Deployment",
  "StatefulSet",
  "DaemonSet",
  "Job",
  "CronJob",
  "Service",
  "ConfigMap",
  "Secret",
  "PersistentVolumeClaim",
  "HorizontalPodAutoscaler",
  "Ingress",
];

export const POD_WORKLOAD_TYPES = new Set(["Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"]);

export const EMPTY_CONTAINER = {
  name: "main",
  image: "",
  tag: "latest",
  pullPolicy: "IfNotPresent",
  ports: [8080],
  command: [],
  args: [],
  workingDir: "",
};

export const ENV_VAR_NAME_PLACEHOLDER = "VAR_NAME";
export const NAMED_REF_PLACEHOLDER = "key";

export const EMPTY_ENV_VAR = { name: ENV_VAR_NAME_PLACEHOLDER, value: "" };
export const EMPTY_CONFIGMAP_REF = { name: NAMED_REF_PLACEHOLDER, keys: [] };
export const EMPTY_SECRET_REF = { name: NAMED_REF_PLACEHOLDER, keys: [] };
export const EMPTY_MOUNTED_FILE = { name: "", mountPath: "", configMap: "", subPath: "" };
export const EMPTY_VOLUME_MOUNT = { name: "", mountPath: "", readOnly: false };

export const STORAGE_DEFAULTS = {
  pvcName: "data-pvc",
  pvName: "data-pv",
  volumeMount: { name: "data", mountPath: "/data", readOnly: false },
};

export const EMPTY_STORAGE_EDITS = {
  pvcName: false,
  pvName: false,
  volumeMount: false,
};

function isEmptyVolumeMountRow(row) {
  return !String(row?.name || "").trim() && !String(row?.mountPath || "").trim();
}

export function hasOnlyEmptyVolumeMounts(mounts = []) {
  return !mounts.length || (mounts.length === 1 && isEmptyVolumeMountRow(mounts[0]));
}

export function applyNewPvcStorageDefaults(storage, { enableManualPv = false } = {}) {
  const edits = { ...EMPTY_STORAGE_EDITS, ...(storage.storageEdits || {}) };
  const next = {
    ...storage,
    storageEdits: edits,
    newPvc: { ...(storage.newPvc || {}) },
    advanced: { ...EMPTY_ADVANCED_STORAGE, ...(storage.advanced || {}) },
    volumeMounts: storage.volumeMounts?.length ? [...storage.volumeMounts] : [],
  };

  if (!edits.pvcName && !String(next.newPvc.name || "").trim()) {
    next.newPvc = { ...next.newPvc, name: STORAGE_DEFAULTS.pvcName };
  }

  if (enableManualPv && !edits.pvName && !String(next.advanced.pvName || "").trim()) {
    next.advanced = { ...next.advanced, pvName: STORAGE_DEFAULTS.pvName };
  }

  if (!edits.volumeMount && hasOnlyEmptyVolumeMounts(next.volumeMounts)) {
    next.volumeMounts = [{ ...STORAGE_DEFAULTS.volumeMount }];
  }

  return next;
}

export const EMPTY_ADVANCED_STORAGE = {
  createManualPv: false,
  pvName: "",
  capacity: "1Gi",
  storageType: "hostPath",
  reclaimPolicy: "Retain",
  hostPath: "",
  nfsServer: "",
  nfsPath: "",
  localPath: "",
  nodeName: "",
};

/** Frontend fallback env defaults per template (used when API omits environment). */
export const TEMPLATE_ENVIRONMENT_DEFAULTS = {
  nginx: {
    envVars: [
      { name: "BASE_URL", value: "" },
      { name: "SERVER_NAME", value: "" },
      { name: "LISTEN_PORT", value: "" },
    ],
    configMapRefs: [{ name: "nginx-config", keys: [] }],
    secretRefs: [{ name: "nginx-secrets", keys: [] }],
  },
  apache: {
    envVars: [
      { name: "BASE_URL", value: "" },
      { name: "SERVER_NAME", value: "" },
      { name: "APACHE_PORT", value: "" },
    ],
    configMapRefs: [{ name: "apache-config", keys: [] }],
    secretRefs: [{ name: "apache-secrets", keys: [] }],
  },
  redis: {
    envVars: [
      { name: "REDIS_PASSWORD", value: "" },
      { name: "REDIS_MAXMEMORY", value: "" },
    ],
    configMapRefs: [{ name: "redis-config", keys: [] }],
    secretRefs: [{ name: "redis-secrets", keys: [] }],
  },
  postgres: {
    envVars: [
      { name: "POSTGRES_USER", value: "" },
      { name: "POSTGRES_PASSWORD", value: "changeme" },
      { name: "POSTGRES_DB", value: "app" },
    ],
    configMapRefs: [{ name: "postgres-config", keys: [] }],
    secretRefs: [{ name: "postgres-secrets", keys: [] }],
  },
  mysql: {
    envVars: [
      { name: "MYSQL_ROOT_PASSWORD", value: "changeme" },
      { name: "MYSQL_DATABASE", value: "" },
      { name: "MYSQL_USER", value: "" },
    ],
    configMapRefs: [{ name: "mysql-config", keys: [] }],
    secretRefs: [{ name: "mysql-secrets", keys: [] }],
  },
  mongodb: {
    envVars: [
      { name: "MONGO_INITDB_ROOT_USERNAME", value: "" },
      { name: "MONGO_INITDB_ROOT_PASSWORD", value: "" },
      { name: "MONGO_INITDB_DATABASE", value: "" },
    ],
    configMapRefs: [{ name: "mongo-config", keys: [] }],
    secretRefs: [{ name: "mongo-secrets", keys: [] }],
  },
  rabbitmq: {
    envVars: [
      { name: "RABBITMQ_DEFAULT_USER", value: "" },
      { name: "RABBITMQ_DEFAULT_PASS", value: "" },
      { name: "RABBITMQ_DEFAULT_VHOST", value: "" },
    ],
    configMapRefs: [{ name: "rabbitmq-config", keys: [] }],
    secretRefs: [{ name: "rabbitmq-secrets", keys: [] }],
  },
  elasticsearch: {
    envVars: [
      { name: "discovery.type", value: "single-node" },
      { name: "xpack.security.enabled", value: "false" },
      { name: "ES_JAVA_OPTS", value: "" },
    ],
    configMapRefs: [{ name: "elasticsearch-config", keys: [] }],
    secretRefs: [{ name: "elasticsearch-secrets", keys: [] }],
  },
  grafana: {
    envVars: [
      { name: "GF_SECURITY_ADMIN_USER", value: "" },
      { name: "GF_SECURITY_ADMIN_PASSWORD", value: "" },
      { name: "GF_SERVER_ROOT_URL", value: "" },
    ],
    configMapRefs: [{ name: "grafana-config", keys: [] }],
    secretRefs: [{ name: "grafana-secrets", keys: [] }],
  },
  prometheus: {
    envVars: [
      { name: "PROMETHEUS_RETENTION", value: "" },
      { name: "SCRAPE_INTERVAL", value: "" },
    ],
    configMapRefs: [{ name: "prometheus-config", keys: [] }],
    secretRefs: [{ name: "prometheus-secrets", keys: [] }],
  },
};

function resolveTemplateEnvironment(template) {
  const defaults = TEMPLATE_ENVIRONMENT_DEFAULTS[template.id] || {};
  const fromApi = template.environment || {};
  return {
    ...defaults,
    ...fromApi,
    envVars: fromApi.envVars?.length ? fromApi.envVars : defaults.envVars || [],
    configMapRefs: fromApi.configMapRefs?.length ? fromApi.configMapRefs : defaults.configMapRefs || [],
    secretRefs: fromApi.secretRefs?.length ? fromApi.secretRefs : defaults.secretRefs || [],
    mountedFiles: fromApi.mountedFiles?.length ? fromApi.mountedFiles : defaults.mountedFiles || [],
  };
}

function mergeProbe(base, override = {}) {
  return { ...base, ...override };
}

function generateTemplateLabels(templateId, appName = "") {
  const name = appName || templateId;
  return {
    "app.kubernetes.io/name": name,
    "app.kubernetes.io/instance": name,
    "app.kubernetes.io/component": templateId,
    "app.kubernetes.io/managed-by": "k8s-dashboard",
  };
}

function buildTemplatePrefills(template) {
  const env = template.environment || {};
  const prefills = {
    workloadType: Boolean(template.workloadType),
    containers: Boolean(template.containers?.length),
    resources: Boolean(template.resources),
    networking: Boolean(template.networking),
    healthChecks: Boolean(template.healthChecks),
    scaling: Boolean(template.scaling),
    storage: Boolean(template.storage),
    environment: Boolean(
      env.envVars?.length || env.configMapRefs?.length || env.secretRefs?.length || env.mountedFiles?.length,
    ),
    basics: {
      appName: true,
      description: Boolean(template.description),
      labels: true,
    },
  };
  return prefills;
}

export function ensureRepeatableRows(state) {
  const env = state.environment || {};
  const storage = state.storage || {};

  return {
    ...state,
    environment: {
      ...env,
      envVars: env.envVars?.length ? [...env.envVars] : [{ ...EMPTY_ENV_VAR }],
      configMapRefs: env.configMapRefs?.length ? [...env.configMapRefs] : [{ ...EMPTY_CONFIGMAP_REF }],
      secretRefs: env.secretRefs?.length ? [...env.secretRefs] : [{ ...EMPTY_SECRET_REF }],
      mountedFiles: env.mountedFiles?.length ? [...env.mountedFiles] : [{ ...EMPTY_MOUNTED_FILE }],
      configMapData: env.configMapData || {},
      secretData: env.secretData || {},
    },
    storage: {
      ...storage,
      advanced: { ...EMPTY_ADVANCED_STORAGE, ...(storage.advanced || {}) },
      storageEdits: { ...EMPTY_STORAGE_EDITS, ...(storage.storageEdits || {}) },
      volumeMounts: storage.volumeMounts?.length ? [...storage.volumeMounts] : [{ ...EMPTY_VOLUME_MOUNT }],
    },
    containers: (state.containers?.length ? state.containers : [{ ...EMPTY_CONTAINER }]).map((container) => ({
      ...EMPTY_CONTAINER,
      ...container,
      ports: container.ports?.length ? [...container.ports] : [8080],
    })),
  };
}

export function createEmptyWizardState(defaultClusterId = "") {
  return ensureRepeatableRows({
    templateId: "",
    templatePrefills: null,
    basics: {
      appName: "",
      description: "",
      clusterId: defaultClusterId,
      namespace: "",
      version: "1.0.0",
      labels: {},
      annotations: {},
      ownerTeam: "",
      environment: "",
      criticality: "",
      contactEmail: "",
      tags: [],
    },
    workloadType: "Deployment",
    containers: [{ ...EMPTY_CONTAINER }],
    environment: {
      envVars: [],
      configMapRefs: [],
      secretRefs: [],
      mountedFiles: [],
      configMapData: {},
      secretData: {},
    },
    resources: {
      cpuRequest: "250m",
      cpuLimit: "500m",
      memoryRequest: "512Mi",
      memoryLimit: "1Gi",
    },
    storage: {
      pvcMode: "none",
      existingPvc: "",
      newPvc: { name: "", storageClass: "", accessMode: "ReadWriteOnce", size: "1Gi" },
      volumeMounts: [],
      advanced: { ...EMPTY_ADVANCED_STORAGE },
      storageEdits: { ...EMPTY_STORAGE_EDITS },
    },
    networking: {
      service: { enabled: true, name: "", type: "ClusterIP", port: 80, targetPort: 80, protocol: "TCP" },
      ingress: { enabled: false, name: "", host: "", path: "/", tlsEnabled: false, tlsSecret: "" },
    },
    healthChecks: {
      readiness: { enabled: false, type: "http", path: "/", port: 80, command: "", initialDelaySeconds: 5, periodSeconds: 10 },
      liveness: { enabled: false, type: "http", path: "/", port: 80, command: "", initialDelaySeconds: 15, periodSeconds: 20 },
      startup: { enabled: false, type: "http", path: "/", port: 80, command: "", initialDelaySeconds: 10, periodSeconds: 10 },
    },
    scaling: {
      replicas: 1,
      cronSchedule: "0 * * * *",
      hpa: { enabled: false, minReplicas: 1, maxReplicas: 5, cpuThreshold: 80, memoryThreshold: null },
    },
    changeSummary: "",
    advancedYamlEdit: false,
    editedYaml: "",
  });
}

export function applyTemplate(state, template) {
  const resolvedEnvironment = resolveTemplateEnvironment(template);
  const resolvedTemplate = { ...template, environment: resolvedEnvironment };
  const appName = resolvedTemplate.basics?.appName || resolvedTemplate.id;
  const next = {
    ...state,
    templateId: resolvedTemplate.id,
    templatePrefills: buildTemplatePrefills(resolvedTemplate),
  };

  if (resolvedTemplate.workloadType) next.workloadType = resolvedTemplate.workloadType;
  if (resolvedTemplate.containers) {
    next.containers = resolvedTemplate.containers.map((c) => ({
      ...EMPTY_CONTAINER,
      ...c,
      ports: c.ports?.length ? [...c.ports] : [8080],
    }));
  }
  if (resolvedTemplate.resources) next.resources = { ...next.resources, ...resolvedTemplate.resources };
  if (resolvedEnvironment.envVars?.length || resolvedEnvironment.configMapRefs?.length || resolvedEnvironment.secretRefs?.length) {
    next.environment = {
      ...next.environment,
      ...resolvedEnvironment,
      envVars: (resolvedEnvironment.envVars || []).map((v) =>
        v.valueFrom
          ? { name: v.name || "", valueFrom: v.valueFrom }
          : { name: v.name || "", value: v.value ?? "" },
      ),
      configMapRefs: resolvedEnvironment.configMapRefs || [],
      secretRefs: resolvedEnvironment.secretRefs || [],
      mountedFiles: resolvedEnvironment.mountedFiles?.length
        ? resolvedEnvironment.mountedFiles
        : next.environment.mountedFiles,
    };
  }
  if (resolvedTemplate.networking) {
    const servicePort =
      resolvedTemplate.networking.service?.port ??
      resolvedTemplate.containers?.[0]?.ports?.[0] ??
      next.networking.service.port;
    const targetPort = resolvedTemplate.networking.service?.targetPort ?? servicePort;
    next.networking = {
      service: {
        ...next.networking.service,
        ...(resolvedTemplate.networking.service || {}),
        port: servicePort,
        targetPort,
        enabled: resolvedTemplate.networking.service?.enabled ?? true,
        type: resolvedTemplate.networking.service?.type || "ClusterIP",
      },
      ingress: { ...next.networking.ingress, ...(resolvedTemplate.networking.ingress || {}) },
    };
  }
  if (resolvedTemplate.scaling) next.scaling = { ...next.scaling, ...resolvedTemplate.scaling };
  if (resolvedTemplate.storage) next.storage = { ...next.storage, ...resolvedTemplate.storage };
  if (resolvedTemplate.healthChecks) {
    next.healthChecks = {
      readiness: mergeProbe(next.healthChecks.readiness, resolvedTemplate.healthChecks.readiness),
      liveness: mergeProbe(next.healthChecks.liveness, resolvedTemplate.healthChecks.liveness),
      startup: mergeProbe(next.healthChecks.startup, resolvedTemplate.healthChecks.startup),
    };
  }

  const primaryPort = next.containers?.[0]?.ports?.[0];
  if (primaryPort) {
    ["readiness", "liveness", "startup"].forEach((probeKey) => {
      const probe = next.healthChecks[probeKey];
      if (probe.enabled && probe.type === "http" && !resolvedTemplate.healthChecks?.[probeKey]?.port) {
        next.healthChecks[probeKey] = { ...probe, port: primaryPort };
      }
    });
    if (next.networking.service.enabled && !resolvedTemplate.networking?.service?.port) {
      next.networking.service = {
        ...next.networking.service,
        port: primaryPort,
        targetPort: primaryPort,
      };
    }
  }

  next.basics = {
    ...next.basics,
    appName: appName,
    description: resolvedTemplate.description || next.basics.description,
    labels: generateTemplateLabels(resolvedTemplate.id, appName),
  };

  return ensureRepeatableRows(next);
}

function sanitizeEnvVars(envVars = []) {
  return envVars
    .map((v) => {
      const name = String(v.name || "").trim();
      const vf = v.valueFrom || {};
      // A reference-sourced var keeps its valueFrom and drops the plain value.
      if (vf.kind && vf.name && vf.key) {
        return { name, valueFrom: { kind: vf.kind, name: vf.name, key: vf.key } };
      }
      return { name, value: v.value ?? "" };
    })
    .filter((v) => {
      if (!v.name || v.name === ENV_VAR_NAME_PLACEHOLDER) return false;
      if (v.valueFrom) return true;
      return Boolean(String(v.value ?? "").trim());
    });
}

function sanitizeNamedRefs(refs = []) {
  return refs.filter((r) => {
    const name = String(r.name || "").trim();
    if (!name) return false;
    const hasKeys = Array.isArray(r.keys) && r.keys.some((k) => String(k).trim());
    if (name === NAMED_REF_PLACEHOLDER && !hasKeys) return false;
    return true;
  });
}

function sanitizeMountedFiles(files = []) {
  return files.filter((f) => String(f.name || f.mountPath || "").trim());
}

function sanitizeVolumeMounts(mounts = []) {
  return mounts.filter((m) => String(m.name || m.mountPath || "").trim());
}

function sanitizeLabels(labels = {}) {
  return Object.fromEntries(Object.entries(labels).filter(([key]) => String(key).trim()));
}

export function buildWizardPayload(state) {
  return {
    basics: {
      ...state.basics,
      labels: sanitizeLabels(state.basics.labels),
    },
    workloadType: state.workloadType,
    containers: state.containers,
    environment: {
      ...state.environment,
      envVars: sanitizeEnvVars(state.environment.envVars),
      configMapRefs: sanitizeNamedRefs(state.environment.configMapRefs),
      secretRefs: sanitizeNamedRefs(state.environment.secretRefs),
      mountedFiles: sanitizeMountedFiles(state.environment.mountedFiles),
    },
    resources: state.resources,
    storage: (() => {
      const { storageEdits, ...storagePayload } = state.storage || {};
      return {
        ...storagePayload,
        newPvc: {
          ...(state.storage.newPvc || {}),
          storageClass: state.storage.advanced?.createManualPv
            ? ""
            : (state.storage.newPvc?.storageClass || ""),
        },
        volumeMounts: sanitizeVolumeMounts(state.storage.volumeMounts),
      };
    })(),
    networking: state.networking,
    healthChecks: state.healthChecks,
    scaling: state.scaling,
    changeSummary: state.changeSummary,
  };
}
