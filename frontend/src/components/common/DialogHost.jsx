import { useEffect, useId, useState, useSyncExternalStore } from "react";
import { useEscapeToClose } from "../../hooks/useEscapeToClose.js";
import { getDialogEntries, promptBlocker, registerDialogHost, settleDialog, subscribeDialogs } from "../../lib/dialogs.js";
import { installDialogFocus } from "../../lib/dialogFocus.js";

// Draws the dialogs asked for with confirmDialog() / promptDialog()
// (lib/dialogs.js), newest on top. Mounted once, in AppShell. It also starts
// the app-wide focus manager (lib/dialogFocus.js) that keeps Tab inside
// whichever modal is on top.

function HostedDialog({ entry }) {
  const { id, options } = entry;
  const isPrompt = options.kind === "prompt";
  const danger = options.tone === "danger";
  const titleId = useId();
  const messageId = useId();
  const inputId = useId();
  const [value, setValue] = useState(options.initialValue || "");
  const blocker = isPrompt ? promptBlocker(options, value) : "";
  // Say why only once something was typed: an empty required box speaks
  // for itself through the disabled button.
  const showBlocker = Boolean(blocker && value !== "" && options.validate);

  const cancel = () => settleDialog(id, isPrompt ? null : false);
  const confirm = () => {
    if (blocker) return;
    settleDialog(id, isPrompt ? value : true);
  };

  useEscapeToClose(cancel);

  const onKeyDown = (event) => {
    if (event.key !== "Enter" || event.defaultPrevented || event.nativeEvent.isComposing) return;
    // A focused button answers Enter itself (Cancel stays Cancel).
    if (event.target instanceof HTMLButtonElement) return;
    event.preventDefault();
    confirm();
  };

  return (
    <div className="modal-overlay app-dialog-overlay" role="presentation" onClick={cancel}>
      <section
        className="card modal-panel confirm-action-modal app-dialog"
        role={danger && !isPrompt ? "alertdialog" : "dialog"}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={options.message ? messageId : undefined}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <header className="modal-header">
          <h3 id={titleId}>{options.title}</h3>
          <button type="button" className="modal-close" onClick={cancel} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" /></svg>
          </button>
        </header>
        {options.message ? (
          <p id={messageId} className="confirm-action-modal__message app-dialog__message">{options.message}</p>
        ) : null}
        {isPrompt ? (
          <div className="confirm-action-modal__field">
            {options.label ? <label htmlFor={inputId}>{options.label}</label> : null}
            <input
              id={inputId}
              type="text"
              value={value}
              placeholder={options.placeholder || undefined}
              aria-label={options.label ? undefined : options.title}
              aria-invalid={showBlocker ? "true" : undefined}
              aria-required={options.required ? "true" : undefined}
              autoComplete="off"
              spellCheck={false}
              autoFocus
              onChange={(event) => setValue(event.target.value)}
            />
            {showBlocker ? <p className="field-hint app-dialog__hint">{blocker}</p> : null}
          </div>
        ) : null}
        <div className="modal-actions">
          <button type="button" className="btn-text" onClick={cancel} autoFocus={danger && !isPrompt}>
            {options.cancelLabel}
          </button>
          <button
            type="button"
            className={danger ? "btn-primary btn-danger" : "btn-primary"}
            onClick={confirm}
            disabled={Boolean(blocker)}
            autoFocus={!danger && !isPrompt}
          >
            {options.confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

export default function DialogHost() {
  const entries = useSyncExternalStore(subscribeDialogs, getDialogEntries, getDialogEntries);
  useEffect(() => registerDialogHost(), []);
  useEffect(() => installDialogFocus(document), []);
  return entries.map((entry) => <HostedDialog key={entry.id} entry={entry} />);
}
