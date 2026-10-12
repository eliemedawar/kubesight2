import { confirmDialog } from "../lib/dialogs.js";

/**
 * The question the router asks before leaving unsaved edits behind.
 *
 * Resolves true to leave, false to stay. Kept apart from the router so the
 * prompt can change without touching navigation. The in-app dialog falls back
 * to the browser's confirm() when no dialog host is mounted.
 *
 * Danger tone: focus starts on Stay, so a reflexive Enter keeps the draft.
 */
export function askToLeave(message) {
  return confirmDialog({
    title: "Leave without saving?",
    message,
    confirmLabel: "Leave",
    cancelLabel: "Stay",
    tone: "danger",
  });
}
