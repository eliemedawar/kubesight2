// --- In-app confirm / prompt dialogs ----------------------------------------
// The browser's own confirm() and prompt() boxes look nothing like the app,
// block the whole tab, and can't be styled as destructive. These two functions
// replace them: call them from any handler and `await` the answer, with no
// per-component state. One <DialogHost /> (components/common/DialogHost.jsx,
// mounted once in AppShell) draws whatever is asked here, newest on top, each
// on the shared Escape stack.

const subscribers = new Set();
let entries = [];
let hostCount = 0;
let nextId = 1;

function publish() {
  for (const fn of subscribers) fn(entries);
}

function str(value, fallback = "") {
  return value == null || value === "" ? fallback : String(value);
}

/** Fill in the defaults of a confirmDialog() call. A bare string is the message. */
export function normalizeConfirmOptions(input) {
  const options = typeof input === "string" ? { message: input } : { ...(input || {}) };
  const danger = options.tone === "danger";
  return {
    kind: "confirm",
    title: str(options.title, "Are you sure?"),
    message: str(options.message),
    confirmLabel: str(options.confirmLabel, danger ? "Delete" : "Confirm"),
    cancelLabel: str(options.cancelLabel, "Cancel"),
    tone: danger ? "danger" : "default",
  };
}

/** Fill in the defaults of a promptDialog() call. A bare string is the message. */
export function normalizePromptOptions(input) {
  const options = typeof input === "string" ? { message: input } : { ...(input || {}) };
  return {
    kind: "prompt",
    title: str(options.title, "Enter a value"),
    message: str(options.message),
    label: str(options.label),
    placeholder: str(options.placeholder),
    initialValue: str(options.initialValue),
    required: Boolean(options.required),
    validate: typeof options.validate === "function" ? options.validate : null,
    confirmLabel: str(options.confirmLabel, "OK"),
    cancelLabel: str(options.cancelLabel, "Cancel"),
    tone: options.tone === "danger" ? "danger" : "default",
  };
}

/**
 * Why a prompt's value can't be confirmed yet, or "" when it can. A required
 * prompt refuses an empty (or blank) value; `validate(value)` may return a
 * message of its own (e.g. "Type the build's name to delete it").
 */
export function promptBlocker(options, value) {
  const text = value == null ? "" : String(value);
  if (options?.required && !text.trim()) return "A value is required.";
  if (options?.validate) {
    const verdict = options.validate(text);
    if (typeof verdict === "string" && verdict) return verdict;
    if (verdict === false) return "This value can't be used.";
  }
  return "";
}

function ask(options, fallback) {
  // No host on screen (a test, or before sign-in): fall back to the browser
  // box rather than wait forever for an answer nobody can give.
  if (hostCount === 0) return Promise.resolve(fallback(options));
  return new Promise((resolve) => {
    entries = [...entries, { id: nextId++, options, resolve }];
    publish();
  });
}

function nativeConfirm(options) {
  if (typeof window === "undefined" || typeof window.confirm !== "function") return false;
  return window.confirm([options.title, options.message].filter(Boolean).join("\n\n"));
}

function nativePrompt(options) {
  if (typeof window === "undefined" || typeof window.prompt !== "function") return null;
  const value = window.prompt([options.title, options.message].filter(Boolean).join("\n\n"), options.initialValue);
  if (value === null) return null;
  return promptBlocker(options, value) ? null : value;
}

/**
 * Ask the user to confirm something, in an in-app dialog.
 *
 *   if (!(await confirmDialog({
 *     title: "Delete template",
 *     message: `Delete "${name}"? This cannot be undone.`,
 *     confirmLabel: "Delete",
 *     tone: "danger",
 *   }))) return;
 *
 * @param {string|{title?: string, message?: string, confirmLabel?: string,
 *   cancelLabel?: string, tone?: "danger"|"default"}} options A bare string is
 *   the message. `tone: "danger"` draws a red confirm button and starts focus
 *   on Cancel, so Enter alone never destroys anything; otherwise focus starts
 *   on the confirm button and Enter confirms.
 * @returns {Promise<boolean>} true when confirmed; false on Cancel, Escape,
 *   the close button or a click on the backdrop.
 */
export function confirmDialog(options) {
  return ask(normalizeConfirmOptions(options), nativeConfirm);
}

/**
 * Ask the user for a line of text, in an in-app dialog.
 *
 *   const reason = await promptDialog({
 *     title: "Reject bundle",
 *     label: "Reason (optional)",
 *     confirmLabel: "Reject",
 *     tone: "danger",
 *   });
 *   if (reason === null) return; // cancelled
 *
 * @param {string|{title?: string, message?: string, label?: string,
 *   placeholder?: string, initialValue?: string, required?: boolean,
 *   validate?: (value: string) => string|boolean, confirmLabel?: string,
 *   cancelLabel?: string, tone?: "danger"|"default"}} options Focus starts in
 *   the input and Enter confirms. `required` keeps the confirm button off
 *   while the value is blank; `validate` may return a message that does the
 *   same (shown under the input).
 * @returns {Promise<string|null>} the value as typed (not trimmed), or null
 *   when cancelled.
 */
export function promptDialog(options) {
  return ask(normalizePromptOptions(options), nativePrompt);
}

// --- Host side ----------------------------------------------------------------

/** The dialogs waiting for an answer, oldest first. */
export function getDialogEntries() {
  return entries;
}

export function subscribeDialogs(fn) {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

/** Answer one dialog (true/false for a confirm, string/null for a prompt) and drop it. */
export function settleDialog(id, value) {
  const entry = entries.find((item) => item.id === id);
  if (!entry) return;
  entries = entries.filter((item) => item !== entry);
  publish();
  entry.resolve(value);
}

/** A host announces itself while mounted; returns the matching unregister. */
export function registerDialogHost() {
  hostCount += 1;
  let registered = true;
  return () => {
    if (!registered) return;
    registered = false;
    hostCount -= 1;
    // The last host went away (sign-out): answer what is still open with
    // "cancel" so no handler waits forever.
    if (hostCount === 0 && entries.length) {
      const pending = entries;
      entries = [];
      publish();
      for (const entry of pending) entry.resolve(entry.options.kind === "prompt" ? null : false);
    }
  };
}
