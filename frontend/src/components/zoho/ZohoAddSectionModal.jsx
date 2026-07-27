import { useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoLayoutDiff from "./ZohoLayoutDiff.jsx";
import { createZohoSection, planZohoLayout } from "../../api/zohoApi.js";

/**
 * Add a layout section — name it, preview the exact whole-layout write, confirm.
 *
 * The preview step is not ceremony: Zoho's only section API replaces the entire
 * layout, so this is where the operator confirms nothing else moves.
 */
export default function ZohoAddSectionModal({ sections = [], onClose, onSaved }) {
  const [name, setName] = useState("");
  const [fieldId, setFieldId] = useState("");
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const preview = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setPlan(await planZohoLayout({ sectionName: name.trim(), fieldId }));
    } catch (err) {
      setError(err.message || "Could not preview the change.");
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    setBusy(true);
    setError("");
    try {
      await createZohoSection(name.trim(), fieldId);
      onSaved(name.trim());
    } catch (err) {
      setError(err.message || "Could not add the section.");
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-zh-modal--wide"
        role="dialog"
        aria-modal="true"
        aria-label="Add a section"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h3>{plan ? "Confirm the layout change" : "Add a section"}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {error ? <ErrorBanner message={error} /> : null}

        {plan ? (
          <>
            <ZohoLayoutDiff plan={plan} />
            <div className="modal-actions">
              <button type="button" className="secondary" onClick={() => setPlan(null)}>
                Back
              </button>
              <button
                type="button"
                className="primary"
                onClick={commit}
                disabled={busy || !plan.writesEnabled}
                title={
                  plan.writesEnabled
                    ? undefined
                    : "Layout writes are disabled — set ZOHO_LAYOUT_WRITE_ENABLED=true on the server."
                }
              >
                {busy ? "Saving…" : `Replace layout & add “${name.trim()}”`}
              </button>
            </div>
          </>
        ) : (
          <form className="sg-zh-form" onSubmit={preview}>
            <label className="sg-zh-form-full">
              Section name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={50}
                placeholder="e.g. Approval details"
                autoFocus
                required
              />
              <span className="field-hint">
                Sections belong to the layout document, and Zoho only lets you replace the whole
                layout at once — so KubeSight shows you the exact change before saving anything.
              </span>
            </label>
            <label className="sg-zh-form-full">
              First field
              <select value={fieldId} onChange={(e) => setFieldId(e.target.value)} required>
                <option value="">Select a field to move</option>
                {sections.map((section) => (
                  <optgroup key={section.id || section.name} label={section.name}>
                    {(section.fields || []).map((field) => (
                      <option key={field.id} value={field.id}>
                        {field.label || field.apiName || field.id}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <span className="field-hint">
                Zoho does not allow empty sections. The selected field will move from its current
                section into the new one as part of the same reviewed layout change.
              </span>
            </label>
            <div className="modal-actions">
              <button type="button" className="secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                type="submit"
                className="primary"
                disabled={busy || !name.trim() || !fieldId}
              >
                {busy ? "Checking…" : "Preview change"}
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
