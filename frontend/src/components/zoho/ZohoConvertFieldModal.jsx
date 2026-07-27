import { useEffect, useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoOptionSourceForm from "./ZohoOptionSourceForm.jsx";
import useZohoOptionSources from "./useZohoOptionSources.js";
import { IconAlert } from "./icons.jsx";
import { linesToValues } from "./zohoFieldMeta";
import { convertZohoField, planZohoFieldConversion } from "../../api/zohoApi.js";

const STEPS = ["The dropdown", "What it breaks", "Done"];

/**
 * Turn a free-text field into a dropdown.
 *
 * Zoho cannot change a field's type in place, so this is really "create a
 * replacement and retire the original" — and the replacement gets a DIFFERENT
 * `cf_` api name. Step 2 exists solely to put that in front of the operator:
 * the Desk webhook and every KubeSight setting keyed on the old name go stale
 * with no error anywhere.
 */
export default function ZohoConvertFieldModal({ field, onClose, onSaved }) {
  const { sources, parentsFor } = useZohoOptionSources();
  const [plan, setPlan] = useState(null);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  const [form, setForm] = useState({
    label: "",
    required: Boolean(field.required),
    mode: "manual",
    values: "",
    sourceKind: "namespaces",
    parentFieldId: "",
    repointConfig: true,
    retireOld: false,
  });
  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  useEffect(() => {
    let cancelled = false;
    planZohoFieldConversion(field.id)
      .then((data) => {
        if (cancelled) return;
        setPlan(data);
        setForm((prev) => ({ ...prev, label: data.suggestedLabel || prev.label }));
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Could not read the field.");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [field.id]);

  const impact = plan?.impact || {};
  const configKeys = impact.configKeys || [];
  const jenkinsParams = impact.jenkinsParams || [];
  const writesEnabled = plan?.layoutWritesEnabled;

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const payload = {
        label: form.label.trim(),
        required: form.required,
        repointConfig: form.repointConfig,
        retireOld: form.retireOld,
      };
      if (form.mode === "manual") payload.values = linesToValues(form.values);
      else {
        payload.sourceKind = form.sourceKind;
        if (form.parentFieldId) payload.parentFieldId = form.parentFieldId;
      }
      setResult(await convertZohoField(field.id, payload));
      setStep(2);
    } catch (err) {
      setError(err.message || "Could not convert the field.");
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
        aria-label={`Convert ${field.label} to a dropdown`}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h3>Convert “{field.label}” to a dropdown</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <ol className="sg-zh-steps">
          {STEPS.map((name, index) => (
            <li
              key={name}
              className={`sg-zh-step${index === step ? " sg-zh-step--on" : ""}${
                index < step ? " sg-zh-step--done" : ""
              }`}
            >
              {name}
            </li>
          ))}
        </ol>

        {error ? <ErrorBanner message={error} /> : null}

        {step === 0 ? (
          <>
            <p className="sg-zh-note">
              <IconAlert />
              <span>
                Zoho can't change a field's type, so KubeSight creates a <b>new</b> dropdown
                beside “{field.label}” — with a new <code>cf_</code> api name. The next step
                lists what that affects.
              </span>
            </p>
            <div className="sg-zh-srcbox">
              <label className="sg-zh-form-full">
                Label for the dropdown
                <input
                  value={form.label}
                  onChange={(e) => set("label", e.target.value)}
                  placeholder={plan?.suggestedLabel || ""}
                  maxLength={80}
                  required
                />
                <span className="field-hint">
                  Zoho requires unique field labels, so it can't reuse “{field.label}” while
                  the original is still on the layout.
                </span>
              </label>

              <div className="routing-options sg-zh-srcmode">
                <button
                  type="button"
                  className={`routing-option ${form.mode === "manual" ? "active" : ""}`}
                  onClick={() => set("mode", "manual")}
                >
                  <b>Type the options</b>
                  <span>A fixed list you maintain here.</span>
                </button>
                <button
                  type="button"
                  className={`routing-option ${form.mode === "live" ? "active" : ""}`}
                  onClick={() => set("mode", "live")}
                >
                  <b>Live source</b>
                  <span>Republished from Kubernetes on every sync.</span>
                </button>
              </div>

              {form.mode === "manual" ? (
                <label className="sg-zh-form-full">
                  Dropdown options (one per line)
                  <textarea
                    rows={6}
                    className="mono"
                    value={form.values}
                    onChange={(e) => set("values", e.target.value)}
                    placeholder={"option-a\noption-b"}
                  />
                  <span className="field-hint">
                    <code>-None-</code> is kept automatically as the first option.
                  </span>
                </label>
              ) : (
                <ZohoOptionSourceForm
                  fieldId={field.id}
                  fieldLabel={form.label || field.label}
                  sources={sources}
                  parentsFor={parentsFor}
                  value={{ sourceKind: form.sourceKind, parentFieldId: form.parentFieldId }}
                  onChange={(next) =>
                    setForm((prev) => ({
                      ...prev,
                      sourceKind: next.sourceKind,
                      parentFieldId: next.parentFieldId,
                    }))
                  }
                />
              )}

              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.required}
                  onChange={(e) => set("required", e.target.checked)}
                />
                Required field
              </label>
            </div>

            <div className="modal-actions">
              <button type="button" className="secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                type="button"
                className="primary"
                onClick={() => setStep(1)}
                disabled={busy || !form.label.trim()}
              >
                {busy ? "Loading…" : "Next — review impact"}
              </button>
            </div>
          </>
        ) : null}

        {step === 1 ? (
          <>
            <p className="sg-zh-note">
              <IconAlert />
              <span>
                The Zoho Desk workflow that posts DevOps Request tickets sends{" "}
                <code>{plan?.field?.apiName}</code>. After this it must send the new field's
                api name instead — KubeSight can't change that for you.
              </span>
            </p>

            <div className="sg-zh-impact">
              <h4>KubeSight settings keyed on {plan?.field?.apiName}</h4>
              {configKeys.length ? (
                <>
                  <ul className="sg-zh-impact-list">
                    {configKeys.map((key) => (
                      <li key={key.key} className="sg-zh-impact-row">
                        <span>{key.label}</span>
                        <code>{key.key}</code>
                      </li>
                    ))}
                  </ul>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={form.repointConfig}
                      onChange={(e) => set("repointConfig", e.target.checked)}
                    />
                    Point {configKeys.length === 1 ? "it" : "them"} at the new field for me
                  </label>
                </>
              ) : (
                <p className="muted">Nothing — no KubeSight setting names this field.</p>
              )}
            </div>

            {jenkinsParams.length ? (
              <div className="sg-zh-impact">
                <h4>Jenkins parameters that pass this field</h4>
                <ul className="sg-zh-impact-list">
                  {jenkinsParams.map((entry) => (
                    <li key={`${entry.where}-${entry.param}`} className="sg-zh-impact-row">
                      <span>
                        {entry.param} <span className="muted">in {entry.where}</span>
                      </span>
                      <code>{entry.value}</code>
                    </li>
                  ))}
                </ul>
                <p className="field-hint">
                  Left as they are — the Jenkins job may still expect the old parameter, so
                  that edit is yours to make on the Source tab.
                </p>
              </div>
            ) : null}

            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={form.retireOld}
                disabled={!writesEnabled}
                onChange={(e) => set("retireOld", e.target.checked)}
              />
              Remove “{field.label}” from the layout now
            </label>
            <p className="field-hint">
              {writesEnabled
                ? "Its historical ticket data is kept — the field just leaves this form. Safer to keep it until the Desk workflow sends the new key."
                : "Needs layout writes, which are disabled on the server (ZOHO_LAYOUT_WRITE_ENABLED)."}
            </p>

            <div className="modal-actions">
              <button type="button" className="secondary" onClick={() => setStep(0)}>
                Back
              </button>
              <button type="button" className="primary" onClick={submit} disabled={busy}>
                {busy ? "Converting…" : `Create “${form.label.trim()}”`}
              </button>
            </div>
          </>
        ) : null}

        {step === 2 && result ? (
          <>
            <p>
              <b>{result.newField?.label}</b> created as a dropdown
              {result.newField?.apiName ? (
                <>
                  {" "}
                  — its api name is <code>{result.newField.apiName}</code>
                </>
              ) : null}
              .
            </p>
            <ul className="sg-zh-impact-list">
              {(result.warnings || []).map((warning) => (
                <li key={warning} className="sg-zh-impact-row sg-zh-impact-row--warn">
                  <span>{warning}</span>
                </li>
              ))}
            </ul>
            <div className="modal-actions">
              <button
                type="button"
                className="primary"
                onClick={() => onSaved(`“${result.newField?.label}” created as a dropdown.`)}
              >
                Done
              </button>
            </div>
          </>
        ) : null}
      </section>
    </div>
  );
}
