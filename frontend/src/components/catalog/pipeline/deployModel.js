/**
 * The Deploy stage's model: its blank target, the manifest its form generates,
 * how it reads in one line, and what a save would refuse.
 *
 * Pure functions, like stageModel.js. The backend (services/ci/deploy_config.py)
 * is the validator; these say out loud, before Save, what it would say after.
 */

/** Where the image goes in the generated manifest. The executor sets the image
 * on the target container whatever the text says — this is for the reader. */
export const IMAGE_PLACEHOLDER = "${IMAGE}";

const DNS_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const DNS_SUBDOMAIN = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/;
const QUANTITY = /^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|k|M|G|T)?$/;

export const SERVICE_TYPES = [
  { value: "ClusterIP", label: "Inside the cluster" },
  { value: "NodePort", label: "On every node's IP" },
];

export const blankCreateForm = () => ({
  port: 8080,
  replicas: 1,
  cpuRequest: "100m",
  cpuLimit: "",
  memoryRequest: "256Mi",
  memoryLimit: "512Mi",
  env: {},
  customManifest: false,
  service: { enabled: true, port: 80, type: "ClusterIP" },
});

export const blankDeploy = () => {
  const deploy = {
    clusterId: "",
    namespace: "",
    deploymentName: "",
    containerName: "",
    // Empty = the image this build pushed.
    image: "",
    createIfMissing: true,
    create: blankCreateForm(),
    manifest: "",
  };
  return { ...deploy, manifest: generateManifest(deploy) };
};

/** A YAML scalar that means exactly the string, whatever it contains. */
const quote = (value) => JSON.stringify(String(value ?? ""));

/**
 * The Deployment (+ Service) the form describes. Deterministic, so the same
 * form always produces the same text and an untouched stage never reads as
 * edited.
 */
export function generateManifest(deploy) {
  const name = deploy?.deploymentName || "my-app";
  const container = deploy?.containerName || name;
  const form = { ...blankCreateForm(), ...(deploy?.create || {}) };
  const service = { ...blankCreateForm().service, ...(form.service || {}) };
  const lines = [
    "apiVersion: apps/v1",
    "kind: Deployment",
    "metadata:",
    `  name: ${name}`,
    "  labels:",
    `    app: ${name}`,
    "spec:",
    `  replicas: ${Number(form.replicas ?? 1)}`,
    "  selector:",
    "    matchLabels:",
    `      app: ${name}`,
    "  template:",
    "    metadata:",
    "      labels:",
    `        app: ${name}`,
    "    spec:",
    "      containers:",
    `        - name: ${container}`,
    `          image: ${quote(IMAGE_PLACEHOLDER)}`,
  ];
  if (form.port) {
    lines.push("          ports:", `            - containerPort: ${Number(form.port)}`);
  }
  const env = Object.entries(form.env || {}).filter(([key]) => key);
  if (env.length) {
    lines.push("          env:");
    for (const [key, value] of env) {
      lines.push(`            - name: ${key}`, `              value: ${quote(value)}`);
    }
  }
  const requests = [
    ["cpu", form.cpuRequest],
    ["memory", form.memoryRequest],
  ].filter(([, value]) => value);
  const limits = [
    ["cpu", form.cpuLimit],
    ["memory", form.memoryLimit],
  ].filter(([, value]) => value);
  if (requests.length || limits.length) {
    lines.push("          resources:");
    if (requests.length) {
      lines.push("            requests:");
      for (const [key, value] of requests) lines.push(`              ${key}: ${quote(value)}`);
    }
    if (limits.length) {
      lines.push("            limits:");
      for (const [key, value] of limits) lines.push(`              ${key}: ${quote(value)}`);
    }
  }
  if (service.enabled && form.port) {
    lines.push(
      "---",
      "apiVersion: v1",
      "kind: Service",
      "metadata:",
      `  name: ${name}`,
      "spec:",
      `  type: ${service.type || "ClusterIP"}`,
      "  selector:",
      `    app: ${name}`,
      "  ports:",
      `    - port: ${Number(service.port || 80)}`,
      `      targetPort: ${Number(form.port)}`
    );
  }
  return `${lines.join("\n")}\n`;
}

/** The patch for a change to the deploy target, keeping a generated manifest
 * in step with the form — unless someone has edited the manifest by hand. */
export function patchDeploy(deploy, patch) {
  const next = { ...deploy, ...patch };
  if (patch.create) next.create = { ...deploy.create, ...patch.create };
  if (!next.create?.customManifest) next.manifest = generateManifest(next);
  return next;
}

/** "prod / payments / payments-api" — the target in one line. */
export function deployTarget(deploy) {
  if (!deploy?.clusterId && !deploy?.deploymentName) return "";
  return [deploy.clusterId || "?", deploy.namespace || "?", deploy.deploymentName || "?"].join(" / ");
}

export function deploySummary(stage) {
  const target = deployTarget(stage.deploy);
  return target ? `→ ${target}` : "No target picked yet";
}

/**
 * What a save would refuse about this stage's target. Mirrors
 * deploy_config.normalize + check_manifest.
 */
export function deployProblems(deploy) {
  const problems = [];
  const add = (message) => problems.push({ field: "deploy", message });
  if (!deploy) {
    add("Pick the cluster, namespace and deployment this stage deploys to.");
    return problems;
  }
  if (!deploy.clusterId) add("Pick a cluster to deploy to.");
  if (!deploy.namespace) add("Pick a namespace.");
  else if (!DNS_LABEL.test(deploy.namespace) || deploy.namespace.length > 63) {
    add("The namespace must be lowercase letters, digits and '-'.");
  }
  if (!deploy.deploymentName) add("Pick a deployment, or name the one to create.");
  else if (!DNS_SUBDOMAIN.test(deploy.deploymentName) || deploy.deploymentName.length > 253) {
    add("The deployment name must be lowercase letters, digits, '-' and '.'.");
  }
  if (deploy.containerName && !DNS_LABEL.test(deploy.containerName)) {
    add("The container name must be lowercase letters, digits and '-'.");
  }
  if (deploy.image && /\s/.test(deploy.image)) add("An image reference cannot contain spaces.");
  const form = deploy.create || {};
  for (const [key, label] of [
    ["cpuRequest", "CPU request"],
    ["cpuLimit", "CPU limit"],
    ["memoryRequest", "memory request"],
    ["memoryLimit", "memory limit"],
  ]) {
    if (deploy.createIfMissing && form[key] && !QUANTITY.test(String(form[key]))) {
      add(`The ${label} “${form[key]}” is not a Kubernetes quantity (250m, 1, 512Mi, 1Gi).`);
    }
  }
  if (deploy.createIfMissing && !String(deploy.manifest || "").trim()) {
    add("Creating the deployment needs a manifest.");
  }
  return problems;
}

/** Kinds the KubeSight server runs itself, after the runner is done — the
 * frontend copy of models_ci.SERVER_STAGE_TYPES. */
export const SERVER_STAGE_KINDS = {
  deploy: "Deploy",
  approval: "Approval",
  store_upload: "App store upload",
};

export const isServerStage = (stage) => Boolean(SERVER_STAGE_KINDS[stage?.stageType]);

/** Server stages run after the build, so nothing a runner executes may follow
 * one; they may follow each other in any order (an Approval before a Deploy is
 * the point). The problem is put on the stage that is in the wrong place. */
export function orderProblem(stage, index, stages) {
  if (isServerStage(stage)) return null;
  const first = stages.findIndex((other, position) => position < index && isServerStage(other));
  if (first < 0) return null;
  const kind = SERVER_STAGE_KINDS[stages[first].stageType];
  return {
    field: "kind",
    message: `Comes after the ${kind} stage “${stages[first].name || `stage ${first + 1}`}”. ${kind} stages run on the KubeSight server once the build has finished, so they must be last — move this stage above it.`,
  };
}

/** The outcome of a Deploy stage in a build, in words and a tone. */
export function deployOutcome(state) {
  if (!state) return null;
  switch (state.outcome) {
    case "deployed":
      return { tone: "success", label: "Deployed" };
    case "unchanged":
      return { tone: "success", label: "Already up to date" };
    case "rolled_back":
      return { tone: "error", label: "Rolled back" };
    case "rollback_failed":
      return { tone: "error", label: "Rollback failed" };
    case "skipped":
      return { tone: "muted", label: "Not deployed" };
    case "cancelled":
      return { tone: "muted", label: "Cancelled" };
    case "failed":
      return { tone: "error", label: "Not deployed" };
    default:
      break;
  }
  switch (state.phase) {
    case "waiting_approval":
      return { tone: "warn", label: "Waiting for approval" };
    case "rolling_out":
      return { tone: "info", label: "Rolling out" };
    case "rolling_back":
      return { tone: "warn", label: "Rolling back" };
    default:
      return { tone: "info", label: "Starting" };
  }
}
