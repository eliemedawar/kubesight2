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
            ×
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
