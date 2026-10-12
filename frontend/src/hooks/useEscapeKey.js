import { useEscapeToClose } from "./useEscapeToClose.js";

/**
 * Calls `onEscape` when Escape is pressed while `active` and this dialog is on
 * top of the app-wide dialog stack. `blocked` (a save/apply in flight) keeps it
 * on top but ignores the key, so a dialog never disappears mid-request.
 */
export default function useEscapeKey(active, onEscape, blocked = false) {
  useEscapeToClose(onEscape, !blocked, Boolean(active));
}
