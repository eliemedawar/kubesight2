import { useEffect, useState } from "react";

export default function EditCatalogModal({ open, catalog, onClose, onSave, saving, error }) {
  const [form, setForm] = useState({
    displayName: "",
    ownerTeam: "",
    environment: "",
    criticality: "",
    description: "",
    documentationUrl: "",
    contactEmail: "",
    tags: "",
  });

  useEffect(() => {
    if (open && catalog) {
      setForm({
        displayName: catalog.displayName || "",
        ownerTeam: catalog.ownerTeam || "",
        environment: catalog.environment || "",
        criticality: catalog.criticality || "",
        description: catalog.description || "",
        documentationUrl: catalog.documentationUrl || "",
        contactEmail: catalog.contactEmail || "",
        tags: (catalog.tags || []).join(", "),
      });
    }
  }, [open, catalog]);

  if (!open) return null;

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section className="card modal-panel" role="dialog" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h3>Edit Catalog Metadata</h3>
          <button type="button" className="modal-close" onClick={onClose}><svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg></button>
        </header>
        {error ? <p className="banner-message error">{error}</p> : null}
        <form
          className="add-app-form"
          onSubmit={(e) => {
            e.preventDefault();
            onSave({
              ...form,
              tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
            });
          }}
        >
          <label>Display Name<input value={form.displayName} onChange={(e) => setForm((p) => ({ ...p, displayName: e.target.value }))} /></label>
          <label>Owner / Team<input value={form.ownerTeam} onChange={(e) => setForm((p) => ({ ...p, ownerTeam: e.target.value }))} /></label>
          <label>Environment<input value={form.environment} onChange={(e) => setForm((p) => ({ ...p, environment: e.target.value }))} /></label>
          <label>Criticality<input value={form.criticality} onChange={(e) => setForm((p) => ({ ...p, criticality: e.target.value }))} /></label>
          <label>Description<textarea rows={2} value={form.description} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} /></label>
          <label>Documentation URL<input value={form.documentationUrl} onChange={(e) => setForm((p) => ({ ...p, documentationUrl: e.target.value }))} /></label>
          <label>Contact Email<input value={form.contactEmail} onChange={(e) => setForm((p) => ({ ...p, contactEmail: e.target.value }))} /></label>
          <label>Tags<input value={form.tags} onChange={(e) => setForm((p) => ({ ...p, tags: e.target.value }))} /></label>
          <div className="modal-actions">
            <button type="button" className="btn-text" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary" disabled={saving}>{saving ? "Saving..." : "Save"}</button>
          </div>
        </form>
      </section>
    </div>
  );
}
