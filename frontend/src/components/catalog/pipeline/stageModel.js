/**
 * The pipeline editor's model: what a stage is, what each kind consumes, how
 * it reads in one line, and what a save would refuse.
 *
 * Pure functions only, so the rules the editor shows are the rules the tests
 * lock. The backend (services/ci/pipelines.py) is still the validator — these
 * only say out loud, before Save, what it would say after.
 */

import { blankDeploy, deployProblems, deploySummary, orderProblem } from "./deployModel.js";

export const MIN_TIMEOUT_SECONDS = 30;
export const MAX_TIMEOUT_SECONDS = 24 * 3600;
export const DEFAULT_TIMEOUT_SECONDS = 1800;
export const MAX_STAGES = 40;
export const MAX_PARAMETERS = 25;

/** Environment-variable shaped: what a parameter or condition name must be. */
export const ENV_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** The kinds a stage can be given, in the order a pipeline uses them. */
export const STAGE_KINDS = [
  {
    value: "checkout",
    label: "Checkout",
    verb: "Get the source",
    description: "Clone the repository at the branch or tag the build was started for.",
    icon: "source",
  },
  {
    value: "command",
    label: "Commands",
    verb: "Run commands",
    description: "Install, compile, test, scan or deploy — any shell steps in a container image.",
    icon: "terminal",
  },
  {
    value: "container_image",
    label: "Container image",
    verb: "Build an image",
    description: "Build the Dockerfile with BuildKit, optionally scan it, and push it to the registry.",
    icon: "image",
  },
  {
    value: "deploy",
    label: "Deploy",
    verb: "Deploy to a cluster",
    description:
      "Roll the image out to a deployment in one of your clusters — created from a manifest if it is missing.",
    icon: "rocket",
  },
];

export const kindOf = (stageType) =>
  STAGE_KINDS.find((kind) => kind.value === stageType) || null;

/**
 * Which fields a stage kind actually consumes at run time. Offering the rest
 * is a lie the runner then ignores: a checkout runs a fixed script in the
 * worker image, so its `image` and `commands` go nowhere, and artifacts are
 * skipped for container_image stages (the image IS the artifact).
 * Keep this in step with runners/kubernetes.py.
 */
export const STAGE_FIELDS = {
  // hostAliases is offered wherever a stage reaches the network — a checkout
  // clones from a git host, so it needs name resolution too.
  checkout: new Set(["runner", "hostAliases"]),
  command: new Set([
    "runner",
    "image",
    "workdir",
    "hostAliases",
    "commands",
    "env",
    "secrets",
    "artifacts",
  ]),
  container_image: new Set(["runner", "workdir", "hostAliases", "env", "imageScan"]),
  // Runs on the KubeSight server, not a runner: no image, no shell, no env.
  deploy: new Set(["deploy"]),
  publish_artifact: new Set([]),
  scan: new Set([]),
};

export const fieldsFor = (stageType) => STAGE_FIELDS[stageType] || STAGE_FIELDS.command;

// Everything the new kind will not use, cleared on the way — otherwise a value
// typed under one kind lingers invisibly in the saved pipeline.
const CLEARED_BY_FIELD = {
  hostAliases: { hostAliases: [] },
  image: { image: "" },
  workdir: { workingDirectory: "" },
  commands: { commands: [] },
  env: { env: {} },
  secrets: { secretRefs: [] },
  artifacts: { artifacts: [] },
  // Cleared when the stage stops being an image stage: a gate left behind on a
  // command stage would be rejected on save, and would read as protection that
  // is not there until then.
  imageScan: { imageScan: null },
  deploy: { deploy: null },
};

const FIELD_NAMES = {
  hostAliases: "host aliases",
  image: "container image",
  workdir: "working directory",
  commands: "commands",
  env: "variables",
  secrets: "secrets",
  artifacts: "files to keep",
  imageScan: "image scan",
  deploy: "deployment target",
};

const hasValue = (stage, field) => {
  switch (field) {
    case "hostAliases":
      return (stage.hostAliases || []).length > 0;
    case "image":
      return Boolean(stage.image);
    case "workdir":
      return Boolean(stage.workingDirectory);
    case "commands":
      return (stage.commands || []).some((line) => String(line).trim());
    case "env":
      return Object.keys(stage.env || {}).length > 0;
    case "secrets":
      return (stage.secretRefs || []).length > 0;
    case "artifacts":
      return (stage.artifacts || []).length > 0;
    case "imageScan":
      return Boolean(stage.imageScan);
    case "deploy":
      return Boolean(stage.deploy?.clusterId || stage.deploy?.deploymentName);
    default:
      return false;
  }
};

/** The patch that changes a stage's kind, clearing what the new kind ignores. */
export function changeKindPatch(stageType) {
  const next = fieldsFor(stageType);
  const patch = { stageType };
  for (const [field, cleared] of Object.entries(CLEARED_BY_FIELD)) {
    if (!next.has(field)) Object.assign(patch, cleared);
  }
  // A Deploy stage is nothing without a target, so it arrives with a blank one.
  if (stageType === "deploy") patch.deploy = blankDeploy();
  return patch;
}

/** What switching kinds would throw away — asked about before it happens. */
export function fieldsLostOnKindChange(stage, stageType) {
  const next = fieldsFor(stageType);
  return Object.keys(CLEARED_BY_FIELD)
    .filter((field) => !next.has(field) && hasValue(stage, field))
    .map((field) => FIELD_NAMES[field]);
}

export const blankStage = (stageType = "command") => ({
  name: "",
  stageType,
  runnerType: "",
  runnerLabels: [],
  image: "",
  workingDirectory: "",
  commands: [],
  env: {},
  secretRefs: [],
  artifacts: [],
  hostAliases: [],
  runCondition: null,
  // Null, not a default object: only an image stage has an image to gate.
  imageScan: null,
  deploy: stageType === "deploy" ? blankDeploy() : null,
  timeoutSeconds: DEFAULT_TIMEOUT_SECONDS,
  continueOnFailure: false,
  enabled: true,
});

/** A name nobody else in the pipeline has — names must be unique on save. */
export function uniqueStageName(base, stages, ignoreIndex = -1) {
  const taken = new Set(
    stages
      .filter((_, index) => index !== ignoreIndex)
      .map((stage) => String(stage.name || "").trim().toLowerCase())
  );
  const root = String(base || "Stage").slice(0, 110);
  if (!taken.has(root.toLowerCase())) return root;
  for (let suffix = 2; ; suffix += 1) {
    const candidate = `${root} ${suffix}`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
}

/** Default names for a new stage of each kind. */
export const defaultStageName = (stageType) =>
  ({ checkout: "Checkout", command: "Run commands", container_image: "Build image", deploy: "Deploy" })[
    stageType
  ] || "Stage";

/**
 * `registry.areeba.com/alpine/git:2.45` → `alpine/git:2.45`.
 *
 * The registry host is the part that is identical on every stage and the part
 * that pushed the useful bit off the end of a narrow row.
 */
export function shortImage(ref) {
  const value = String(ref || "").trim();
  if (!value) return "";
  const parts = value.split("/");
  if (parts.length > 1 && (/[.:]/.test(parts[0]) || parts[0] === "localhost")) {
    parts.shift();
  }
  return parts.join("/");
}

/** First line of the script that says what the stage does. */
export function firstCommand(stage) {
  for (const raw of stage.commands || []) {
    const line = String(raw || "").trim();
    if (!line || line.startsWith("#") || line === "set -e" || line === "set -eu" ||
      line.startsWith("set -o")) {
      continue;
    }
    return line;
  }
  return "";
}

export const commandCount = (stage) =>
  (stage.commands || []).filter((line) => String(line || "").trim()).length;

/** The image stage's three build inputs live in env; these are their keys. */
export const IMAGE_ENV_KEYS = {
  name: "IMAGE_NAME",
  tag: "IMAGE_TAG",
  dockerfile: "DOCKERFILE_PATH",
};

const IMAGE_ENV_KEY_SET = new Set(Object.values(IMAGE_ENV_KEYS));

/** The variables an image stage shows in the generic editor — its own three
 * build inputs have first-class fields and are left out of the list. */
export const plainEnv = (stage) => {
  if (stage.stageType !== "container_image") return stage.env || {};
  return Object.fromEntries(
    Object.entries(stage.env || {}).filter(([key]) => !IMAGE_ENV_KEY_SET.has(key))
  );
};

/** Put the generic variables back without disturbing the image build inputs. */
export const mergePlainEnv = (stage, nextPlain) => {
  if (stage.stageType !== "container_image") return nextPlain;
  const kept = Object.fromEntries(
    Object.entries(stage.env || {}).filter(([key]) => IMAGE_ENV_KEY_SET.has(key))
  );
  return { ...nextPlain, ...kept };
};

/** Armed = a scan actually gates this stage's push. Absent and
 * `{enabled: false}` are both "not armed", but they are different answers to
 * "was this image scanned?" and the editor shows them differently. */
export const scanArmed = (stage) =>
  Boolean(stage.imageScan && stage.imageScan.enabled !== false);

/** One line: what this stage does. Shown under its name in the flow. */
export function stageSummary(stage) {
  if (stage.stageType === "checkout") return "Clones the repository";
  if (stage.stageType === "container_image") {
    const dockerfile = stage.env?.[IMAGE_ENV_KEYS.dockerfile] || "Dockerfile";
    const where = stage.workingDirectory ? ` in ${stage.workingDirectory}` : "";
    return `Builds ${dockerfile}${where}`;
  }
  if (stage.stageType === "deploy") return deploySummary(stage);
  if (stage.stageType === "publish_artifact") return "Unsupported — publish artifact";
  if (stage.stageType === "scan") return "Unsupported — security scan";
  return firstCommand(stage) || "No commands yet";
}

export function conditionSummary(condition) {
  if (!condition?.variable) return "Always";
  const verb = condition.operator === "not_equals" ? "is not" : "is";
  return `When ${condition.variable} ${verb} “${condition.value ?? ""}”`;
}

export function timeoutLabel(seconds) {
  const value = Number(seconds) || DEFAULT_TIMEOUT_SECONDS;
  if (value < 60) return `${value}s`;
  if (value < 3600) {
    const minutes = Math.round((value / 60) * 10) / 10;
    return `${minutes} min`;
  }
  const hours = Math.floor(value / 3600);
  const minutes = Math.round((value % 3600) / 60);
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
}

/**
 * Small facts about a stage that change how a build behaves. The flow shows
 * them as marks next to the stage so they are visible without opening it.
 */
export function stageFlags(stage) {
  const flags = [];
  if (stage.enabled === false) {
    flags.push({ key: "off", icon: "power", label: "Turned off — skipped by every build" });
  }
  if (stage.runCondition?.variable) {
    flags.push({ key: "when", icon: "branch", label: conditionSummary(stage.runCondition) });
  }
  if (stage.continueOnFailure) {
    flags.push({ key: "continue", icon: "forward", label: "Keeps going if this stage fails" });
  }
  if (stage.stageType === "container_image" && scanArmed(stage)) {
    flags.push({ key: "scan", icon: "shield", label: "Image is scanned before it is pushed" });
  }
  if (stage.stageType === "deploy" && stage.deploy?.createIfMissing) {
    flags.push({ key: "create", icon: "plus", label: "Creates the deployment if it is missing" });
  }
  if ((stage.secretRefs || []).length) {
    const count = stage.secretRefs.length;
    flags.push({ key: "secrets", icon: "key", label: `${count} secret${count === 1 ? "" : "s"}` });
  }
  return flags;
}

/**
 * What a save would refuse for this stage, or what would make it fail every
 * build. Mirrors normalize_stage + _apply_stages + _run_condition — each one
 * is a message the backend would otherwise return after Save.
 */
export function stageProblems(stage, index, stages, parameters) {
  const problems = [];
  const name = String(stage.name || "").trim();
  if (!name) {
    problems.push({ field: "name", message: "Give this stage a name." });
  } else {
    const clash = stages.some(
      (other, position) =>
        position !== index &&
        String(other.name || "").trim().toLowerCase() === name.toLowerCase()
    );
    if (clash) {
      problems.push({ field: "name", message: `Another stage is also called “${name}”. Names must be unique.` });
    }
  }
  // Refused even when the stage is off: the backend validates every stage.
  if (stage.stageType === "command" && !commandCount(stage)) {
    problems.push({ field: "commands", message: "Add at least one command to run." });
  }
  if (stage.stageType === "deploy") {
    problems.push(...deployProblems(stage.deploy));
  }
  const misplaced = orderProblem(stage, index, stages);
  if (misplaced) problems.push(misplaced);
  if (stage.stageType === "publish_artifact" || stage.stageType === "scan") {
    problems.push({
      field: "kind",
      message: "This stage kind has no executor and can no longer be saved. Change its kind or remove it.",
    });
  }
  const timeout = Number(stage.timeoutSeconds);
  if (
    stage.timeoutSeconds !== "" &&
    stage.timeoutSeconds !== undefined &&
    (!Number.isFinite(timeout) || timeout < MIN_TIMEOUT_SECONDS || timeout > MAX_TIMEOUT_SECONDS)
  ) {
    problems.push({ field: "timeout", message: "The timeout must be between 30 seconds and 24 hours." });
  }
  const variable = stage.runCondition?.variable;
  if (variable) {
    const declared = (parameters || []).some((param) => param.name === variable);
    if (!declared) {
      problems.push({
        field: "condition",
        message: `Runs only when “${variable}” matches, but there is no build input called that — the stage would never run as intended.`,
        level: "warning",
      });
    }
  }
  return problems;
}

/** What a save would refuse about a build input. Mirrors _parameters. */
export function parameterProblems(param, index, parameters) {
  const problems = [];
  const name = String(param.name || "").trim();
  if (!name) {
    problems.push({ field: "name", message: "Give this input a name." });
  } else if (!ENV_NAME_RE.test(name)) {
    problems.push({
      field: "name",
      message: "Letters, digits and underscores only, not starting with a digit — it becomes an environment variable.",
    });
  } else if (parameters.some((other, position) => position !== index && other.name === name)) {
    problems.push({ field: "name", message: `“${name}” is defined twice.` });
  }
  if (param.type === "choice") {
    const options = (param.choices || []).map((item) => String(item).trim()).filter(Boolean);
    if (!options.length) {
      problems.push({ field: "choices", message: "A choice needs at least one option." });
    } else if (param.default && !options.includes(String(param.default).trim())) {
      problems.push({ field: "default", message: "The default must be one of the options." });
    }
  }
  return problems;
}

/**
 * Stages as the linter should see them: numbered by where they are NOW.
 *
 * The linter echoes each stage's `position` back as `stagePosition`. A saved
 * stage carries its stored (0-based, pre-reorder) position and a new one none
 * at all, so sending them as-is pins findings to the wrong stage the moment
 * anything moves.
 */
export const stagesForLint = (stages) =>
  stages.map((stage, index) => ({ ...stage, position: index + 1 }));

/** Lint findings keyed by stage index — see stagesForLint for the numbering. */
export function lintByStage(lint) {
  const out = new Map();
  for (const finding of lint?.findings || []) {
    if (finding.level === "info") continue;
    const index = Number(finding.stagePosition) - 1;
    if (!Number.isInteger(index) || index < 0) continue;
    if (!out.has(index)) out.set(index, []);
    out.get(index).push(finding);
  }
  return out;
}

// --- Change tracking ---------------------------------------------------------
// "Unsaved changes" is derived from the saved copy rather than a flag set on
// every keystroke: typing a value back to what it was is no longer a change,
// and the flow can mark exactly which stages are edited.

const comparable = (stage) => {
  const { id, _key, position, createdAt, updatedAt, ...rest } = stage || {};
  return JSON.stringify({
    ...rest,
    timeoutSeconds: Number(rest.timeoutSeconds) || DEFAULT_TIMEOUT_SECONDS,
    commands: (rest.commands || []).map((line) => String(line)),
    runnerType: rest.runnerType || "",
    image: rest.image || "",
    workingDirectory: rest.workingDirectory || "",
    enabled: rest.enabled !== false,
    runCondition: rest.runCondition?.variable ? rest.runCondition : null,
  });
};

/**
 * Which saved stage a draft stage is. The editor stamps every stage with a
 * client `_key` when it loads, because a generated (starter) pipeline's
 * stages have no database id — matching on id alone calls every one of them
 * "new" and "removed" the moment the tab opens.
 */
const identity = (stage) => stage?._key ?? (stage?.id != null ? `id:${stage.id}` : null);

let keySeed = 0;
/** Stamp a stage with a fresh client identity. */
export const withKey = (stage) => ({ ...stage, _key: `k${(keySeed += 1)}` });

/** The stage as the API should see it — without the editor's own identity. */
// eslint-disable-next-line no-unused-vars
export const forApi = ({ _key, ...stage }) => stage;

/** Per-stage edit state against the saved pipeline: "new" | "edited" | null. */
export function stageChanges(stages, savedStages) {
  const savedBy = new Map(
    (savedStages || []).filter((s) => identity(s) != null).map((s) => [identity(s), s])
  );
  return stages.map((stage) => {
    const key = identity(stage);
    if (key == null || !savedBy.has(key)) return "new";
    return comparable(stage) === comparable(savedBy.get(key)) ? null : "edited";
  });
}

export function pipelineDiff(stages, parameters, saved) {
  const savedStages = saved?.stages || [];
  const perStage = stageChanges(stages, savedStages);
  const currentIds = new Set(stages.map(identity).filter((key) => key != null));
  const removed = savedStages.filter((stage) => !currentIds.has(identity(stage))).length;
  const savedOrder = savedStages.map(identity).filter((key) => currentIds.has(key));
  const order = stages.map(identity).filter((key) => key != null && savedOrder.includes(key));
  const reordered = order.some((id, index) => id !== savedOrder[index]);
  const paramsChanged =
    JSON.stringify(parameters || []) !== JSON.stringify(saved?.parameters || []);
  const added = perStage.filter((state) => state === "new").length;
  const edited = perStage.filter((state) => state === "edited").length;
  const count = added + edited + removed + (reordered ? 1 : 0) + (paramsChanged ? 1 : 0);
  return { perStage, added, edited, removed, reordered, paramsChanged, count };
}

/** "2 stages edited, 1 added, build inputs changed" — the save bar's line. */
export function describeDiff(diff) {
  const parts = [];
  const plural = (count, word) => `${count} stage${count === 1 ? "" : "s"} ${word}`;
  if (diff.edited) parts.push(plural(diff.edited, "edited"));
  if (diff.added) parts.push(plural(diff.added, "added"));
  if (diff.removed) parts.push(plural(diff.removed, "removed"));
  if (diff.reordered) parts.push("order changed");
  if (diff.paramsChanged) parts.push("build inputs changed");
  return parts.join(", ");
}

/** Which stages a build input steers — shown on the input so renaming one is
 * not done blind. */
export function stagesUsingParameter(name, stages) {
  if (!name) return [];
  return stages
    .map((stage, index) => ({ stage, index }))
    .filter(({ stage }) => stage.runCondition?.variable === name);
}

export function moveItem(items, from, to) {
  if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) {
    return items;
  }
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}
