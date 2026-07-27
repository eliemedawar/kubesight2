import { useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoLayoutDiff from "./ZohoLayoutDiff.jsx";
import { useTicketing } from "../ticketing/TicketingContext.jsx";

export default function ZohoRenameSectionModal({ section, onClose, onSaved }) {
  const { name: providerName, can, api } = useTicketing();
  const [name, setName] = useState(section?.name || "");
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const preview = async (event) => {
    event.preventDefault();
    if (!can("layoutPlan")) {
      await commit();
      return;
    }
    setBusy(true);
    setError("");
    try {
      setPlan(
        await api.planLayout({
          mutations: [
            {
              kind: "rename_section",
              sectionId: String(section.id),
              name: name.trim(),
            },
          ],
        })
      );
    } catch (err) {
      setError(err.message || "Could not preview the section rename.");
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    setBusy(true);
    setError("");
    try {
      await api.renameSection(section.id, name.trim());
      onSaved(name.trim());
    } catch (err) {
      setError(err.message || "Could not rename the section.");
      setBusy(false);
    }
  };

  const unchanged = name.trim() === String(section?.name || "").trim();

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-zh-modal--wide"
        role="dialog"
        aria-modal="true"
        aria-label="Rename section"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <h3>{plan ? "Confirm the layout change" : "Rename section"}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
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
              >
                {busy ? "Saving…" : `Replace layout & rename to “${name.trim()}”`}
              </button>
            </div>
          </>
        ) : (
          <form className="sg-zh-form" onSubmit={preview}>
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
                Renaming keeps the existing {providerName} section ID and all fields in that
                section.
                {can("layoutPlan")
                  ? " You will preview the exact whole-layout write before saving."
                  : ""}
              </span>
            </label>
            <div className="modal-actions">
              <button type="button" className="secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                type="submit"
                className="primary"
                disabled={busy || !name.trim() || unchanged}
              >
                {busy ? "Saving…" : can("layoutPlan") ? "Preview change" : "Rename section"}
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
