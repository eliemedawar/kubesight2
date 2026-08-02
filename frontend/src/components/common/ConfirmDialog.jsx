import { useEffect, useRef, useState } from "react";

/**
 * Confirmation, with the consequence stated before the button that causes it.
 *
 * Replaces `window.confirm`, which this codebase used for genuinely destructive
 * things — removing an app from inventory, starting an automatic cluster
 * upgrade that drains nodes. `confirm` cannot show a list of what is affected,
 * cannot be styled to distinguish "are you sure" from "this is irreversible",
 * and is dismissed by the same Enter keypress that got the operator there.
 *
 * `requirePhrase` adds the typed-confirmation gate already used for YAML
 * applies: the operator names the target before the button enables. Reserve it
 * for actions whose blast radius is other people's workloads. Everything else
 * gets a plain confirm, because a friction that appears everywhere is read as
 * noise and clicked through.
 *
 * The phrase is cleared whenever the dialog opens. A confirmation left over
 * from last time is not consent for this time.
 */
export default function ConfirmDialog({
  open,
  title,
  body,
  affected,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "danger",
  requirePhrase = "",
  busy = false,
  error = "",
  onConfirm,
  onCancel,
}) {
  const [phrase, setPhrase] = useState("");
  const dialogRef = useRef(null);

  useEffect(() => {
    if (open) {
      setPhrase("");
    }
  }, [open]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const onKeyDown = (event) => {
      if (event.key === "Escape" && !busy) {
        onCancel?.();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, busy, onCancel]);

  // Focus the dialog rather than the confirm button: landing focus on the
  // destructive action means a stray Enter completes it.
  useEffect(() => {
    if (open) {
      dialogRef.current?.focus({ preventScroll: true });
    }
  }, [open]);

  if (!open) {
    return null;
  }

  const phraseSatisfied = !requirePhrase || phrase.trim() === requirePhrase;

  return (
    <div className="modal-backdrop" role="presentation">
      <div
        ref={dialogRef}
        className={`modal confirm-dialog confirm-dialog--${tone}`}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        tabIndex={-1}
      >
        <h3 id="confirm-dialog-title">{title}</h3>
        {body ? <p className="confirm-dialog-body">{body}</p> : null}

        {affected?.length ? (
          <div className="confirm-dialog-affected">
            <p className="muted">This affects:</p>
            <ul>
              {affected.map((item, index) => (
                <li key={typeof item === "string" ? item : index}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {requirePhrase ? (
          <label className="confirm-dialog-phrase">
            <span>
              Type <strong>{requirePhrase}</strong> to confirm
            </span>
            <input
              value={phrase}
              onChange={(event) => setPhrase(event.target.value)}
              placeholder={requirePhrase}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        ) : null}

        {error ? <p className="form-error" role="alert">{error}</p> : null}

        <div className="modal-actions">
          <button type="button" className="btn-ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={tone === "danger" ? "btn-danger" : "primary"}
            onClick={onConfirm}
            disabled={busy || !phraseSatisfied}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
