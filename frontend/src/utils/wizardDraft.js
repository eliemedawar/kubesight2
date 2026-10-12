/**
 * The Cluster Builder wizard's unfinished draft, kept by the page.
 *
 * The page holds the draft in memory so it survives the page's own tabs
 * (Builds, Sources, a build's detail), and mirrors it into sessionStorage so
 * it survives a reload of the browser tab. Per user, so a different sign-in on
 * the same tab never inherits it.
 *
 * Nothing secret goes to storage. The wizard holds none today (credentials
 * live on the Sources tab and are referenced by id), but any key that looks
 * like one is dropped anyway, so a field added later cannot leak by accident.
 */

const STORAGE_PREFIX = "kubesight.clusterBuilder.wizardDraft.v1.";

// Fields the wizard may carry that are only references: keep them.
const SECRET_KEY = /pass(word|phrase)|secret|token|private_?key|api_?key|credential(?!s?_?id$)/i;

// Large or re-readable on the way back in; kept in memory, not in storage.
const MEMORY_ONLY = ["planBuild", "workloadPlan"];

export function draftStorageKey(userId) {
  return userId === null || userId === undefined || userId === "" ? null : `${STORAGE_PREFIX}${userId}`;
}

/** A deep copy with every secret-looking key removed. */
export function scrubSecrets(value) {
  if (Array.isArray(value)) {
    return value.map(scrubSecrets);
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, entry] of Object.entries(value)) {
      if (SECRET_KEY.test(key)) continue;
      out[key] = scrubSecrets(entry);
    }
    return out;
  }
  return value;
}

/** What goes to sessionStorage for a draft. */
export function storableDraft(draft) {
  if (!draft) return null;
  const copy = { ...draft };
  MEMORY_ONLY.forEach((key) => delete copy[key]);
  return scrubSecrets(copy);
}

function isDraft(value) {
  return Boolean(value && typeof value === "object" && value.basics && typeof value.basics === "object");
}

/** Read the stored draft; null when there is none, storage is blocked, or it is unreadable. */
export function readStoredDraft(storage, key) {
  if (!storage || !key) return null;
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return isDraft(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** Write (or with no draft, remove) the stored draft. Storage failures are ignored. */
export function writeStoredDraft(storage, key, draft) {
  if (!storage || !key) return;
  try {
    if (!draft) {
      storage.removeItem(key);
      return;
    }
    storage.setItem(key, JSON.stringify(storableDraft(draft)));
  } catch {
    // Blocked or full storage: the draft still lives in memory for this visit.
  }
}

/** The browser's sessionStorage, or null where it is unavailable. */
export function sessionStore() {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

/** One line naming the draft, for "Continue draft" and the restored notice. */
export function describeDraft(draft, stepLabels = []) {
  if (!draft) return "";
  const name = draft.basics?.name?.trim();
  const label = draft.stepLabel || stepLabels[draft.step];
  const what = name ? `“${name}”` : "an unnamed build";
  return label ? `${what}, stopped at ${label}` : what;
}
