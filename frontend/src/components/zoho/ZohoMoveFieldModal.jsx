import { useMemo, useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoLayoutDiff from "./ZohoLayoutDiff.jsx";
import { useTicketing } from "../ticketing/TicketingContext.jsx";

export default function ZohoMoveFieldModal({
  field,
  currentSectionName,
  sectionNames,
  onClose,
  onSaved,
}) {
  const { name: providerName, can, api } = useTicketing();
  const choices = useMemo(
    () => sectionNames.filter((name) => name !== currentSectionName),
    [currentSectionName, sectionNames]
  );
  const [target, setTarget] = useState(choices[0] || "");
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const move = async () => {
    setBusy(true);
    setError("");
    try {
      await api.moveFieldToSection(field.id, target);
      onSaved(`Field "${field.label}" moved to "${target}".`);
    } catch (err) {
      setError(err.message || "Could not move the field.");
      setBusy(false);
    }
  };

  const submit = async (event) => {
    event.preventDefault();
    if (!can("layoutPlan")) {
      await move();
      return;
    }
    setBusy(true);
    setError("");
    try {
      setPlan(
        await api.planLayout({
          mutations: [
            {
              kind: "place_field",
              fieldId: String(field.id),
              sectionName: target,
            },
          ],
        })
      );
    } catch (err) {
      setError(err.message || "Could not preview the field move.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-zh-modal--wide"
        role="dialog"
        aria-modal="true"
        aria-label={`Move field ${field.label}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <h3>{plan ? "Confirm the layout change" : `Move "${field.label}"`}</h3>
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
                onClick={move}
                disabled={busy || !plan.writesEnabled}
              >
                {busy ? "Moving…" : `Replace layout & move to "${target}"`}
              </button>
            </div>
          </>
        ) : (
          <form className="sg-zh-form" onSubmit={submit}>
            <label className="sg-zh-form-full">
              Destination section
              <select value={target} onChange={(event) => setTarget(event.target.value)} required>
                {choices.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <span className="field-hint">
                The field keeps its API name, type, required state, and options.{" "}
                {can("layoutPlan")
                  ? `Because ${providerName} replaces the whole layout, you will preview the exact write first.`
                  : `Only its position on the ${providerName} form changes.`}
              </span>
            </label>
            <div className="modal-actions">
              <button type="button" className="secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" className="primary" disabled={busy || !target}>
                {busy ? "Checking…" : can("layoutPlan") ? "Preview move" : "Move field"}
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
