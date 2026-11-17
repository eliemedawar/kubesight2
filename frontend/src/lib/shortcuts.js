/**
 * The app's global keyboard shortcuts, all Alt+letter (⌥+letter on a Mac):
 *
 *   Alt+K  jump palette          (AppShell)
 *   Alt+G  Hermes chat, admins   (AppShell)
 *   Alt+C  cluster picker        (Topbar)
 *   Alt+N  namespace picker      (Topbar)
 *
 * Alt, not Ctrl: browsers keep Ctrl+N/T/W for themselves, and Ctrl+K / Ctrl+G
 * are the browser's search-bar and find-next keys.
 */

export const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || "");

/** How to write a shortcut for this keyboard: "Alt K" or "⌥K". */
export function shortcutLabel(letter) {
  return isMac ? `⌥${letter}` : `Alt ${letter}`;
}

function isTextEntry(target) {
  if (!target || typeof Element === "undefined" || !(target instanceof Element)) return false;
  if (target.isContentEditable) return true;
  if (target.tagName === "TEXTAREA") return true;
  if (target.tagName !== "INPUT") return false;
  return !["button", "checkbox", "radio", "range", "submit", "reset", "file", "color"].includes(
    (target.getAttribute("type") || "text").toLowerCase()
  );
}

/**
 * The letter of an Alt+letter shortcut ("K", "G", …), or "" when `event` is
 * not one. Reads `event.code`, not `event.key`: on a Mac ⌥K types "˚".
 * On a Mac, ⌥+letter typed into a text field is left alone (it types accents
 * there); Escape still closes whatever such a key opened.
 */
export function altShortcutLetter(event) {
  if (!event || !event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return "";
  if (event.defaultPrevented) return "";
  const match = /^Key([A-Z])$/.exec(event.code || "");
  if (!match) return "";
  if (isMac && isTextEntry(event.target)) return "";
  return match[1];
}
