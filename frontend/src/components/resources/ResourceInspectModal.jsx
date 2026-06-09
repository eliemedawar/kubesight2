import DataTable from "../common/DataTable.jsx";

export default function ResourceInspectModal({ open, title, loading, error, mode, content, rolloutRows, onClose }) {
  if (!open) {
    return null;
  }

  const rolloutColumns = [
    { key: "revision", label: "Revision" },
    { key: "changeCause", label: "Change cause" },
  ];

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel modal-card--wide resource-inspect-modal"
        role="dialog"
        aria-labelledby="resource-inspect-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <h3 id="resource-inspect-title">{title}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        {loading ? <p className="muted">Loading…</p> : null}
        {!loading && error ? <p className="banner-message error">{error}</p> : null}
        {!loading && !error && mode === "rollout" ? (
          rolloutRows?.length ? (
            <DataTable columns={rolloutColumns} rows={rolloutRows} />
          ) : (
            <p className="muted">No rollout history returned.</p>
          )
        ) : null}
        {!loading && !error && mode !== "rollout" && content ? (
          <pre className="yaml-preview resource-inspect-modal__content">{content}</pre>
        ) : null}
        <footer className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose}>
            Close
          </button>
        </footer>
      </section>
    </div>
  );
}
