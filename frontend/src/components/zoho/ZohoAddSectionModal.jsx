import { useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { useTicketing } from "../ticketing/TicketingContext.jsx";

export default function ZohoAddSectionModal({ onClose, onSaved }) {
  const { formNoun, api } = useTicketing();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createSection({ name: name.trim() });
      await onSaved(`Section "${name.trim()}" added.`);
    } catch (err) {
      setError(err.message || "Could not add the section.");
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Add section"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <h3>Add section</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        {error ? <ErrorBanner message={error} /> : null}

        <form className="sg-zh-form" onSubmit={submit}>
          <label className="sg-zh-form-full">
            Section name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={50}
              autoFocus
              required
            />
            <span className="field-hint">
              The new section is created directly on the configured {formNoun}. You can add or
              move fields into it afterward.
            </span>
          </label>
          <div className="modal-actions">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="primary" disabled={busy || !name.trim()}>
              {busy ? "Adding…" : "Add section"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
