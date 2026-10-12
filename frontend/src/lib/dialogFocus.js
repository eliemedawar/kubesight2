// --- Keyboard focus stays inside the top dialog --------------------------------
// One mechanism for every modal in the app, so the ~70 dialog components don't
// each carry their own trap. It watches the DOM for shown
// `[role="dialog"|"alertdialog"][aria-modal="true"]` elements and keeps a
// stack of them in the order they appeared (newest = top), then:
//   - Tab / Shift+Tab wrap inside the top dialog;
//   - a dialog that opens without taking focus itself gets it (on its own
//     container, so no button is pressed by a stray Enter);
//   - when a dialog closes and focus was left nowhere, focus goes back to what
//     had it before that dialog opened, one layer at a time.
// Popovers and menus (no aria-modal) are left alone. A widget that handles Tab
// itself (an editor that indents) calls preventDefault and is left alone too.

const MODAL_SELECTOR =
  '[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]';

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "area[href]",
  "button",
  "input",
  "select",
  "textarea",
  "iframe",
  "summary",
  "[contenteditable]:not([contenteditable='false'])",
  "[tabindex]",
].join(",");

/**
 * Where Tab should move inside a dialog. `index` is the position of the
 * focused element among the dialog's tabbable elements (-1 when it is not one
 * of them); `insertAt` is then the position of the first tabbable that comes
 * after it in the document (`count` when none does). `inside` says whether
 * focus is inside the dialog at all. Returns the index to focus.
 */
export function trapTargetIndex({ count, index = -1, insertAt = 0, inside = true, shift = false }) {
  if (count <= 0) return -1;
  if (!inside) return shift ? count - 1 : 0;
  if (index >= 0) {
    if (shift) return index === 0 ? count - 1 : index - 1;
    return index === count - 1 ? 0 : index + 1;
  }
  // Focus sits on something inside that is not itself tabbable (the dialog
  // container, a scroll area): step to the neighbour in document order.
  if (shift) return insertAt <= 0 ? count - 1 : insertAt - 1;
  return insertAt >= count ? 0 : insertAt;
}

/**
 * Bring the stack (oldest first) up to date with the modals now shown, in
 * document order. Closed ones drop out; new ones go on top in the order they
 * appear in the document (a dialog and its nested child opened together).
 */
export function reconcileDialogStack(stack, shown) {
  const shownSet = new Set(shown);
  const kept = stack.filter((entry) => shownSet.has(entry.element));
  const known = new Set(kept.map((entry) => entry.element));
  const opened = shown.filter((element) => !known.has(element)).map((element) => ({ element }));
  const closed = stack.filter((entry) => !shownSet.has(entry.element));
  return { stack: [...kept, ...opened], opened, closed };
}

function isShown(element) {
  if (!element.isConnected) return false;
  if (element.closest("[inert]")) return false;
  if (!element.getClientRects().length) return false;
  return getComputedStyle(element).visibility !== "hidden";
}

function isTabbable(element) {
  if (element.matches(":disabled")) return false;
  if (element.tabIndex < 0) return false;
  if (element.closest("[inert]")) return false;
  if (element.tagName === "INPUT" && element.type === "hidden") return false;
  // An unchecked radio is reached with the arrow keys from the checked one.
  if (element.tagName === "INPUT" && element.type === "radio" && !element.checked && element.name) {
    const form = element.form || element.ownerDocument;
    const group = form.querySelectorAll(`input[type="radio"][name="${CSS.escape(element.name)}"]`);
    if (Array.from(group).some((radio) => radio.checked)) return false;
  }
  if (!element.getClientRects().length) return false;
  return getComputedStyle(element).visibility !== "hidden";
}

export function getTabbables(container) {
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(isTabbable);
}

function canTakeFocus(element) {
  return Boolean(element && element.isConnected && typeof element.focus === "function" && isShown(element));
}

function focusContainer(element) {
  if (!element.hasAttribute("tabindex")) element.setAttribute("tabindex", "-1");
  element.focus({ preventScroll: true });
}

/**
 * Start the focus manager on `doc`. Mounted once (by DialogHost); returns the
 * function that stops it.
 */
export function installDialogFocus(doc = document) {
  if (!doc || typeof MutationObserver === "undefined") return () => {};
  let stack = [];
  // What had focus lately, newest last; `null` marks "focus went to the page
  // body" (a click on plain text), so a later dialog doesn't hand focus back
  // to something long forgotten.
  let recent = [];

  const remember = (element) => {
    recent = [...recent.filter((item) => item !== element), element].slice(-12);
  };
  const onFocusIn = (event) => remember(event.target);
  const onFocusOut = (event) => {
    if (!event.relatedTarget) remember(null);
  };

  const returnTargetFor = (dialog) => {
    const active = doc.activeElement;
    if (active && active !== doc.body && !dialog.contains(active)) return active;
    for (let i = recent.length - 1; i >= 0; i -= 1) {
      const item = recent[i];
      if (item === null) return null;
      if (item.isConnected && !dialog.contains(item)) return item;
    }
    return null;
  };

  const focusIsLost = () => {
    const active = doc.activeElement;
    return !active || active === doc.body || !active.isConnected;
  };

  const sync = () => {
    const shown = Array.from(doc.querySelectorAll(MODAL_SELECTOR)).filter(isShown);
    const next = reconcileDialogStack(stack, shown);
    if (!next.opened.length && !next.closed.length) return;
    stack = next.stack;

    for (const entry of next.opened) entry.returnTo = returnTargetFor(entry.element);

    const top = stack[stack.length - 1];
    if (next.opened.length && top) {
      // A dialog that focused something of its own (autoFocus) keeps it.
      if (!top.element.contains(doc.activeElement)) focusContainer(top.element);
      return;
    }

    // Only closes: hand focus back, one layer at a time, unless the app
    // already put it somewhere sensible.
    const lostOrOutside = focusIsLost() || (top && !top.element.contains(doc.activeElement));
    if (!lostOrOutside) return;
    // Several may close at once (a "Discard?" confirm and the editor under
    // it): the newest one whose return target still stands wins.
    const target = next.closed
      .map((entry) => entry.returnTo)
      .reverse()
      .find((element) => canTakeFocus(element) && (!top || top.element.contains(element)));
    if (target) {
      target.focus({ preventScroll: true });
    } else if (top) {
      focusContainer(top.element);
    }
  };

  const onKeyDown = (event) => {
    if (event.key !== "Tab" || event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey) return;
    // The DOM may have changed since the last observer pass: settle first.
    sync();
    const top = stack[stack.length - 1]?.element;
    if (!top || !top.isConnected) return;
    const tabbables = getTabbables(top);
    event.preventDefault();
    if (!tabbables.length) {
      focusContainer(top);
      return;
    }
    const active = doc.activeElement;
    const index = tabbables.indexOf(active);
    let insertAt = tabbables.length;
    if (index < 0 && active && top.contains(active)) {
      const after = tabbables.findIndex(
        // eslint-disable-next-line no-bitwise
        (element) => active.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING
      );
      insertAt = after < 0 ? tabbables.length : after;
    }
    const target = trapTargetIndex({
      count: tabbables.length,
      index,
      insertAt,
      inside: Boolean(active && top.contains(active)),
      shift: event.shiftKey,
    });
    tabbables[target]?.focus();
  };

  const observer = new MutationObserver(sync);
  observer.observe(doc.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["role", "aria-modal", "hidden", "inert"],
  });
  doc.addEventListener("focusin", onFocusIn, true);
  doc.addEventListener("focusout", onFocusOut, true);
  // Bubble phase: an editor's own Tab handler (React, at the root) runs first.
  doc.addEventListener("keydown", onKeyDown);
  if (doc.activeElement && doc.activeElement !== doc.body) remember(doc.activeElement);
  sync();

  return () => {
    observer.disconnect();
    doc.removeEventListener("focusin", onFocusIn, true);
    doc.removeEventListener("focusout", onFocusOut, true);
    doc.removeEventListener("keydown", onKeyDown);
    stack = [];
    recent = [];
  };
}
