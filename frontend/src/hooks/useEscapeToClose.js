import { useEffect, useRef } from "react";

// --- Escape closes the top dialog -------------------------------------------
// Dialogs stack across the whole app: a confirm over a CI drawer, a credential
// form over the Source tab, a nested connection editor over a client. One
// Escape must close only the layer on top, so every open dialog registers in
// this one stack and a single listener asks the newest entry. A key a widget
// inside already handled (an open dropdown calls preventDefault) is left alone,
// so Escape first closes the list and only then the dialog.
const escapeStack = [];

function onEscapeKey(event) {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  const top = escapeStack[escapeStack.length - 1]?.current;
  if (!top) return;
  // Claim the key either way, so an older window-level listener can't close
  // something underneath as well.
  event.preventDefault();
  // A busy top dialog swallows the key rather than passing it to the one
  // underneath: closing the drawer behind a Send in flight is not what was meant.
  if (top.enabled) top.onClose(event);
}

/**
 * Close a dialog on Escape. Pass `enabled: false` while it must stay open (a
 * request in flight) — it still sits on top and swallows the key. Pass
 * `active: false` for a dialog that is mounted but not shown, so it takes no
 * place in the stack. `onClose` and `enabled` are read fresh on every press.
 */
export function useEscapeToClose(onClose, enabled = true, active = true) {
  const entryRef = useRef(null);
  entryRef.current = { onClose, enabled };
  useEffect(() => {
    if (!active) return undefined;
    const entry = entryRef;
    escapeStack.push(entry);
    if (escapeStack.length === 1) document.addEventListener("keydown", onEscapeKey);
    return () => {
      const index = escapeStack.lastIndexOf(entry);
      if (index >= 0) escapeStack.splice(index, 1);
      if (escapeStack.length === 0) document.removeEventListener("keydown", onEscapeKey);
    };
  }, [active]);
}

export default useEscapeToClose;
