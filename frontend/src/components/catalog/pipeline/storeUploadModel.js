/**
 * The App store upload stage's model: its blank target, what each store takes,
 * how it reads in one line, what a save would refuse, and how a build's
 * publish reads in the drawer.
 *
 * Pure functions, like stageModel.js. The backend (services/ci/store_upload_config.py)
 * is the validator; these say out loud, before Save, what it would say after.
 * The track and target names are the ones Mobile Apps publishes to.
 */

export const STORES = [
  { value: "google_play", label: "Google Play" },
  { value: "app_store", label: "App Store Connect" },
];

export const PLAY_TRACKS = [
  { value: "internal", label: "Internal testing" },
  { value: "alpha", label: "Closed testing (alpha)" },
  { value: "beta", label: "Open testing (beta)" },
  { value: "production", label: "Production" },
];

export const APP_STORE_TARGETS = [
  { value: "testflight", label: "TestFlight" },
  { value: "review", label: "TestFlight, then submit for App Review" },
];

export const ARTIFACT_TYPES = {
  google_play: [
    { value: "aab", label: "App bundle (.aab)" },
    { value: "apk", label: "APK (.apk)" },
  ],
  app_store: [{ value: "ipa", label: "iOS app (.ipa)" }],
};

const DEFAULT_TARGET = { google_play: "internal", app_store: "testflight" };
const DEFAULT_ARTIFACT = { google_play: "aab", app_store: "ipa" };
const PATTERN_RE = /^[A-Za-z0-9._*?/[\]-]{1,200}$/;

export const blankStoreUpload = (appId = null) => ({
  appId,
  store: "google_play",
  target: "internal",
  artifactType: "aab",
  artifactPattern: "",
});

/** The app a new stage should publish: the one linked to this service. */
export function defaultAppId(apps, serviceId) {
  const linked = (apps || []).filter((app) => app.ciServiceId === serviceId);
  return linked.length === 1 ? linked[0].id : null;
}

/** Switching store resets what only made sense on the other one. */
export function patchStoreUpload(upload, patch) {
  const next = { ...upload, ...patch };
  if (patch.store && patch.store !== upload?.store) {
    next.target = DEFAULT_TARGET[patch.store];
    next.artifactType = DEFAULT_ARTIFACT[patch.store];
  }
  return next;
}

export function targetLabel(upload) {
  if (!upload?.store) return "";
  if (upload.store === "google_play") return `Google Play (${upload.target || "internal"} track)`;
  return upload.target === "review" ? "App Store Connect (submitted for App Review)" : "App Store Connect (TestFlight)";
}

export function storeUploadSummary(stage, apps) {
  const upload = stage.storeUpload;
  if (!upload?.store) return "No store picked yet";
  const app = (apps || []).find((item) => item.id === upload.appId);
  const name = app?.name || (upload.appId ? `app #${upload.appId}` : "the linked app");
  return `${name} → ${targetLabel(upload)}`;
}

/** What a save would refuse. Mirrors store_upload_config.normalize. */
export function storeUploadProblems(upload) {
  const problems = [];
  const add = (message) => problems.push({ field: "storeUpload", message });
  if (!upload) {
    add("Pick the app and the store this stage publishes to.");
    return problems;
  }
  const store = upload.store || "google_play";
  if (!STORES.some((item) => item.value === store)) add("Pick Google Play or App Store Connect.");
  const targets = store === "app_store" ? APP_STORE_TARGETS : PLAY_TRACKS;
  if (upload.target && !targets.some((item) => item.value === upload.target)) {
    add(store === "app_store" ? "Pick TestFlight or App Review." : "Pick a Google Play track.");
  }
  const types = ARTIFACT_TYPES[store] || [];
  if (upload.artifactType && !types.some((item) => item.value === upload.artifactType)) {
    add(`${store === "app_store" ? "App Store Connect" : "Google Play"} does not take ${String(upload.artifactType).toUpperCase()} files.`);
  }
  if (upload.artifactPattern && !PATTERN_RE.test(upload.artifactPattern)) {
    add("The file pattern may use letters, digits, . - _ / and the wildcards * ? [ ].");
  }
  return problems;
}

/** The outcome of a store upload stage in a build, in words and a tone. */
export function storeUploadOutcome(state) {
  if (!state) return null;
  switch (state.outcome) {
    case "published":
      return { tone: "success", label: "Published" };
    case "timed_out":
      return { tone: "error", label: "Did not finish in time" };
    case "cancelled":
      return { tone: "muted", label: "Cancelled" };
    case "skipped":
      return { tone: "muted", label: "Not published" };
    case "failed":
      return { tone: "error", label: "Not published" };
    default:
      break;
  }
  if (state.phase === "publishing") {
    const status = state.publishStatus || "queued";
    return { tone: "info", label: status === "processing" ? "Store is processing" : "Publishing" };
  }
  return { tone: "info", label: "Preparing" };
}

/** Steps of the Mobile Apps publish, labelled for the drawer. */
export const PUBLISH_STEP_LABELS = {
  credentials: "Credentials",
  upload: "Upload",
  release: "Release",
  confirm: "Confirm",
};
