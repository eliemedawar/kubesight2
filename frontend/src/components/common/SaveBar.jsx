import { useBlocker } from "react-router-dom";
import { useEffect } from "react";
import ConfirmDialog from "./ConfirmDialog.jsx";

/**
 * The unsaved-changes bar, and the guard that stops you walking away from it.
 *
 * The bar itself was already in SettingsPage; it is lifted here because every
 * form screen needs the same thing and the Integrations hub is about to be the
 * second one.
 *
 * The guard is new, and routing is what made it necessary. Settings edits used
 * to live in App state that survived a "page change" because nothing unmounted
 * — the draft was still there when you came back. Now a route change unmounts
 * the page and the draft is gone, so navigating away silently discards work
 * that the bar is simultaneously telling you is unsaved.
 *
 * Two exits are covered: in-app navigation via `useBlocker`, and closing the
 * tab via `beforeunload`. The second cannot be styled or worded — browsers show
 * their own text — but leaving it out means the one exit that loses work with
 * no undo is the one exit with no warning.
 */
export default function SaveBar({
  dirty,
  saving = false,
  onSave,
  onDiscard,
  summary,
  saveLabel = "Save changes",
  discardLabel = "Discard",
  blockNavigation = true,
}) {
  const shouldBlock = Boolean(dirty && blockNavigation && !saving);

  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      shouldBlock && currentLocation.pathname !== nextLocation.pathname
  );

  useEffect(() => {
    if (!shouldBlock) {
      return undefined;
    }
    const onBeforeUnload = (event) => {
      event.preventDefault();
      // Required by Chrome; the string itself is ignored by every modern browser.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [shouldBlock]);

  if (!dirty) {
    return null;
  }

  return (
    <>
      <div className="settings-savebar" role="status">
        <span className="settings-savebar-dot" aria-hidden="true" />
        <span className="settings-savebar-text">
          <strong>Unsaved changes</strong>
          {summary ? ` · ${summary}` : null}
        </span>
        <button type="button" className="btn-ghost" onClick={onDiscard} disabled={saving}>
          {discardLabel}
        </button>
        <button type="button" className="primary" onClick={onSave} disabled={saving}>
          {saving ? "Saving…" : saveLabel}
        </button>
      </div>

      <ConfirmDialog
        open={blocker.state === "blocked"}
        tone="warn"
        title="Leave without saving?"
        body="Your changes have not been saved. Leaving this page will discard them."
        confirmLabel="Leave and discard"
        cancelLabel="Stay on this page"
        onCancel={() => blocker.reset?.()}
        onConfirm={() => blocker.proceed?.()}
      />
    </>
  );
}
