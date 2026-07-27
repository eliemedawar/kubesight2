import { useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoOptionSourceForm from "./ZohoOptionSourceForm.jsx";
import useZohoOptionSources from "./useZohoOptionSources.js";
import { IconAlert } from "./icons.jsx";
import { deleteZohoFieldBinding, setZohoFieldBinding } from "../../api/zohoApi.js";

/**
 * Turn one dropdown's options from a hand-typed list into a live source (or
 * back). This is the same mechanism the Application / Environment / Variable
 * fields have always used, opened up to any picklist.
 *
 * Removing a binding is not destructive: the field keeps whatever options it
 * currently holds — the sync simply stops rewriting them.
 */
export default function ZohoBindingModal({ field, onClose, onSaved }) {
  const { sources, loading, error: catalogError, parentsFor } = useZohoOptionSources();
  const existing = field.binding && !field.binding.locked ? field.binding : null;
  const [mode, setMode] = useState(existing ? "live" : "manual");
  const [draft, setDraft] = useState({
    sourceKind: existing?.sourceKind || "namespaces",
    parentFieldId: existing?.parentFieldId || "",
  });
  const [enabled, setEnabled] = useState(existing ? existing.enabled : true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      if (mode === "manual") {
        if (existing) await deleteZohoFieldBinding(field.id);
        onSaved(
          existing
            ? `“${field.label}” is back to a manual option list.`
            : `“${field.label}” already uses a manual option list.`
        );
        return;
      }
      await setZohoFieldBinding(field.id, {
        sourceKind: draft.sourceKind,
        parentFieldId: draft.parentFieldId || undefined,
        label: field.label,
        enabled,
      });
      onSaved(`“${field.label}” now publishes from a live source on every sync.`);
    } catch (err) {
      setError(err.message || "Could not save the option source.");
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-zh-modal--wide"
        role="dialog"
        aria-modal="true"
        aria-label={`Option source — ${field.label}`}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h3>Option source — {field.label}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {error ? <ErrorBanner message={error} /> : null}
        {catalogError ? <ErrorBanner message={catalogError} /> : null}

        <div className="routing-options sg-zh-srcmode">
          <button
            type="button"
            className={`routing-option ${mode === "manual" ? "active" : ""}`}
            onClick={() => setMode("manual")}
          >
            <b>Manual list</b>
            <span>You type the options; KubeSight never changes them.</span>
          </button>
          <button
            type="button"
            className={`routing-option ${mode === "live" ? "active" : ""}`}
            onClick={() => setMode("live")}
          >
            <b>Live source</b>
            <span>Every sync republishes the options from Kubernetes.</span>
          </button>
        </div>

        {mode === "live" ? (
          loading ? (
            <p className="muted">Loading sources…</p>
          ) : (
            <>
              <ZohoOptionSourceForm
                fieldId={field.id}
                fieldLabel={field.label}
                sources={sources}
                parentsFor={parentsFor}
                value={draft}
                onChange={setDraft}
              />
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                Publish on every sync
              </label>
              <p className="sg-zh-note">
                <IconAlert />
                <span>
                  The sync replaces this field's whole option list. Any value typed by hand in Zoho
                  Desk — or here — is overwritten on the next run.
                </span>
              </p>
            </>
          )
        ) : (
          <p className="muted">
            {existing
              ? "Saving unbinds the field. It keeps the options it holds today, and “Manage options” takes over again."
              : "This field already uses a manual list — edit it with “Manage options”."}
          </p>
        )}

        <div className="modal-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            onClick={save}
            disabled={busy || (mode === "manual" && !existing) || (mode === "live" && loading)}
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </section>
    </div>
  );
}
