export default function ConfirmActionModal({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  busy = false,
  error = "",
  children,
  onClose,
  onConfirm,
}) {
  if (!open) return null;

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section className="card modal-panel confirm-action-modal" role="dialog" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h3>{title}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg>
          </button>
        </header>
        {message ? <p className="confirm-action-modal__message">{message}</p> : null}
        {children}
        {error ? <p className="banner-message error">{error}</p> : null}
        <div className="modal-actions">
          <button type="button" className="btn-text" onClick={onClose} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={danger ? "btn-primary btn-danger" : "btn-primary"}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}
