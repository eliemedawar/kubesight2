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
// deploy_config._ENVIRONMENT_RE
const ENVIRONMENT = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$/;
// deploy_config._VALUE_PATH_RE / _RELEASE_RE
const VALUE_PATH = /^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){0,15}$/;
const MAX_RELEASE = 53;

export const SERVICE_TYPES = [
  { value: "ClusterIP", label: "Inside the cluster" },
  { value: "NodePort", label: "On every node's IP" },
];

export const blankCreateForm = () => ({
  // "form": the manifest generated from the fields below. "template": an
  // inventory template (Inventory → Templates), read when the build deploys.
  // New stages start from the inventory: that is where the deployment's real
  // definition lives. Stages saved before templates existed read as "form".
  source: "template",
  templateId: "",
  templateName: "",
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

/** The stage deploys to the building service's linked deployment (resolved
 * when a build starts) rather than a target saved on the stage. */
export const isLinkedDeploy = (deploy) => deploy?.target === "linked";

/** The stage installs, then upgrades, a release of an Inventory Helm chart. */
export const isHelmDeploy = (deploy) => deploy?.target === "helm";

export const blankHelm = () => ({
  chartTemplateId: "",
  chartName: "",
  // Empty: the chart's current version when the build runs.
  chartVersion: "",
  // A values-*.yaml stored on the chart, used for the first install only.
  valuesFile: "",
  imageValues: { mode: "split", repository: "image.repository", tag: "image.tag" },
});

/**
 * Where a chart most likely takes its image, from its default values: the
 * common image.repository + image.tag pair, or one ``image`` string.
 */
export function guessImageValues(defaultValues) {
  const image = defaultValues?.image;
  if (typeof image === "string") return { mode: "full", path: "image" };
  if (image && typeof image === "object" && "repository" in image) {
    return { mode: "split", repository: "image.repository", tag: "image.tag" };
  }
  return blankHelm().imageValues;
}

// deploy_config.MAX_HELM_IMAGES
export const MAX_HELM_IMAGES = 12;

/** The chart sets several images (``helm.images``) rather than one. */
export const setsSeveralImages = (helm) => helm?.imageSet === "several";

/** One row of a several-images target: where the image comes from, and the
 * chart values it goes into. */
export const blankHelmImage = (stage = "") => ({
  from: "stage",
  stage,
  image: "",
  imageValues: { mode: "split", repository: "", tag: "" },
});

/**
 * Every place a chart's default values take an image: each object holding a
 * ``repository`` (with its ``tag`` beside it), and each string under a key
 * called ``image``. In the order the chart lists them, at most MAX_HELM_IMAGES.
 */
export function findImageValues(defaultValues) {
  const found = [];
  const walk = (node, path) => {
    if (!node || typeof node !== "object" || Array.isArray(node) || found.length >= MAX_HELM_IMAGES) return;
    if (typeof node.repository === "string" && path.length) {
      found.push({ mode: "split", repository: [...path, "repository"].join("."), tag: [...path, "tag"].join(".") });
      return;
    }
    for (const [key, value] of Object.entries(node)) {
      if (!/^[A-Za-z0-9_-]+$/.test(key)) continue;
      if (key === "image" && typeof value === "string") found.push({ mode: "full", path: [...path, key].join(".") });
      else walk(value, [...path, key]);
    }
  };
  walk(defaultValues, []);
  return found.slice(0, MAX_HELM_IMAGES);
}

/**
 * The rows a several-images target starts with: one per image the chart's
 * values hold, paired in order with the pipeline's image stages. Rows past the
 * last image stage start with no stage picked.
 */
export function guessImageRows(defaultValues, imageStageNames) {
  const places = findImageValues(defaultValues);
  const names = imageStageNames || [];
  if (!places.length) {
    return (names.length ? names : [""]).slice(0, MAX_HELM_IMAGES).map((name) => blankHelmImage(name));
  }
  return places.map((imageValues, index) => ({ ...blankHelmImage(names[index] || ""), imageValues }));
}

const imagePaths = (values) =>
  values?.mode === "full" ? [values.path] : [values?.repository, values?.tag];

/** What a save would refuse about where one image goes. */
function imageValuesProblem(values) {
  if (imagePaths(values).some((path) => !VALUE_PATH.test(String(path || "")))) {
    return "Name the chart value(s) the image goes into, like image.repository and image.tag.";
  }
  if (values?.mode !== "full" && values?.repository === values?.tag) {
    return "The repository and the tag must be different chart values.";
  }
  return "";
}

/** deploy_config._helm_images, plus the stage check the pipeline validator makes. */
function helmImagesProblems(rows, stages) {
  const problems = [];
  const add = (message) => problems.push({ field: "deploy", message });
  if (!rows.length) {
    add("Add the images this chart deploys.");
    return problems;
  }
  if (rows.length > MAX_HELM_IMAGES) add(`A stage can set at most ${MAX_HELM_IMAGES} images.`);
  const imageStages = new Set(
    (stages || []).filter((stage) => stage.stageType === "container_image").map((stage) => String(stage.name || "").trim().toLowerCase())
  );
  const owner = new Map();
  rows.forEach((row, index) => {
    const label = `Image ${index + 1}`;
    if ((row.from || "stage") === "stage") {
      if (!row.stage) add(`${label}: pick the image stage it comes from.`);
      else if (stages && !imageStages.has(String(row.stage).trim().toLowerCase())) {
        add(`${label}: there is no “Build an image” stage called “${row.stage}”.`);
      }
    } else if (!String(row.image || "").trim()) add(`${label}: write the image it deploys.`);
    else if (/\s/.test(row.image)) add(`${label}: an image reference cannot contain spaces.`);
    const problem = imageValuesProblem(row.imageValues);
    if (problem) {
      add(`${label}: ${problem}`);
      return;
    }
    for (const path of imagePaths(row.imageValues)) {
      for (const [other, otherIndex] of owner) {
        if (path === other || path.startsWith(`${other}.`) || other.startsWith(`${path}.`)) {
          add(`Images ${otherIndex + 1} and ${index + 1} both set ${path === other ? `“${path}”` : `“${other}” and “${path}”`}. Each image needs values of its own.`);
        }
      }
      owner.set(path, index);
    }
  });
  return problems;
}

/** Switch a stage between a picked target, the service's linked one and a
 * Helm chart. What the other modes had is kept, so switching back loses nothing. */
export function setDeployTargetMode(deploy, mode) {
  const base = deploy || blankDeploy();
  if (mode === "linked") return { ...base, target: "linked", createIfMissing: false };
  if (mode === "helm") {
    return {
      ...base,
      target: "helm",
      createIfMissing: false,
      releaseName: base.releaseName || "",
      helm: { ...blankHelm(), ...(base.helm || {}) },
    };
  }
  const { target: _target, ...rest } = base;
  return { ...rest, target: "fixed" };
}

/** Which of a service's links a linked stage would deploy to — the same
 * choice deployment_links._pick_link makes when a build starts. */
export function pickLinkedDeployment(links, environment) {
  const items = links || [];
  const wanted = String(environment || "").trim().toLowerCase();
  if (wanted) {
    const matching = items.filter((link) => String(link.environment || "").trim().toLowerCase() === wanted);
    if (matching.length === 1) return { link: matching[0], problem: "" };
    return {
      link: null,
      problem: matching.length
        ? `${matching.length} linked deployments are labelled “${environment}”. Give them distinct labels.`
        : `No linked deployment is labelled “${environment}”.`,
    };
  }
  if (items.length === 1) return { link: items[0], problem: "" };
  return {
    link: null,
    problem: items.length
      ? `This service is linked to ${items.length} deployments. Set an environment to say which one.`
      : "This service is not linked to a deployment yet.",
  };
}

/** "prod / payments / payments-api" — the target in one line. */
export function deployTarget(deploy) {
  if (isLinkedDeploy(deploy)) {
    return `the service's linked deployment${deploy.environment ? ` (${deploy.environment})` : ""}`;
  }
  if (isHelmDeploy(deploy)) {
    if (!deploy.clusterId && !deploy.releaseName) return "";
    const chart = deploy.helm?.chartName || deploy.helm?.chartTemplateId;
    return `${[deploy.clusterId || "?", deploy.namespace || "?", deploy.releaseName || "?"].join(" / ")} (Helm${chart ? ` · ${chart}` : ""})`;
  }
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
export function deployProblems(deploy, stages) {
  const problems = [];
  const add = (message) => problems.push({ field: "deploy", message });
  if (!deploy) {
    add("Pick the cluster, namespace and deployment this stage deploys to.");
    return problems;
  }
  if (isLinkedDeploy(deploy)) {
    if (deploy.environment && !ENVIRONMENT.test(deploy.environment)) {
      add("The environment label may hold letters, digits, spaces, dots, dashes and underscores.");
    }
    if (deploy.containerName && !DNS_LABEL.test(deploy.containerName)) {
      add("The container name must be lowercase letters, digits and '-'.");
    }
    if (deploy.image && /\s/.test(deploy.image)) add("An image reference cannot contain spaces.");
    return problems;
  }
  if (isHelmDeploy(deploy)) {
    if (!deploy.clusterId) add("Pick a cluster to deploy to.");
    if (!deploy.namespace) add("Pick a namespace.");
    const release = deploy.releaseName || "";
    if (!release) add("Name the Helm release, or pick one that is installed.");
    else if (!DNS_LABEL.test(release) || release.length > MAX_RELEASE) {
      add(`The release name must be lowercase letters, digits and '-', at most ${MAX_RELEASE} characters.`);
    }
    const helm = deploy.helm || {};
    if (!helm.chartTemplateId) add("Pick the Inventory chart to deploy.");
    if (setsSeveralImages(helm)) {
      problems.push(...helmImagesProblems(helm.images || [], stages));
      return problems;
    }
    const problem = imageValuesProblem(helm.imageValues || {});
    if (problem) add(problem);
    if (deploy.image && /\s/.test(deploy.image)) add("An image reference cannot contain spaces.");
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
  const fromTemplate = deploy.createIfMissing && form.source === "template";
  if (fromTemplate && !form.templateId) add("Pick the inventory template to create the deployment from.");
  if (fromTemplate) return problems;
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
