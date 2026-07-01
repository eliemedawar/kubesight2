import { useEffect, useRef, useState } from "react";

import { importDeploymentForm, validateFormImport } from "../../api/deploymentFormsApi.js";

const LEVEL_ICON = { ok: "✅", warn: "⚠️", error: "❌" };

/**
 * Upload a filled deployment form, show its validation result (✅/⚠️/❌), and open
 * the Deploy Wizard prefilled with the parsed values. Uploading only parses +
 * validates + prefills — nothing deploys here. Deploying, adding to a bundle, or
 * sending for approval all happen from the wizard once the deployment is complete.
 */
export default function ImportDeploymentFormModal({ open, onClose, onContinueInWizard }) {
  const [record, setRecord] = useState(null);
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const fileInputRef = useRef(null);

  useEffect(() => {
    if (open) {
      setRecord(null);
      setBusy(false);
      setAction("");
      setError("");
      setNotice("");
    }
  }, [open]);

  if (!open) return null;

  const validation = record?.validation || {};
  const checks = validation.checks || [];

  const handleFile = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    setNotice("");
    setRecord(null);
    try {
      const data = await importDeploymentForm(file);
      setRecord(data);
    } catch (err) {
      setError(err.message || "Failed to import the deployment form.");
    } finally {
      setBusy(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleRevalidate = async () => {
    if (!record) return;
    setBusy(true);
    setAction("validate");
    setError("");
    try {
      setRecord(await validateFormImport(record.id));
      setNotice("Re-validated against the current cluster.");
    } catch (err) {
      setError(err.message || "Re-validation failed.");
    } finally {
      setBusy(false);
      setAction("");
    }
  };

  const handleContinue = () => {
    if (!record) return;
    onContinueInWizard?.(record);
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>Import Deployment Form</h3>
          <p className="muted">
            Upload a filled Excel deployment form. Kubesight parses and validates it against the
            current template and cluster, then opens the Deploy Wizard prefilled with your values.
            Review, complete anything left to Kubesight (like volumes), and deploy or add to a
            bundle from the wizard. Nothing deploys on upload.
          </p>
        </div>

        {error ? <p className="banner-message error">{error}</p> : null}
        {notice ? <p className="banner-message success">{notice}</p> : null}

        <section className="form-section">
          <h4>Deployment form (.xlsx)</h4>
          <input
            ref={fileInputRef}
            type="file"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={handleFile}
            disabled={busy}
          />
        </section>

        {busy && !record ? <p className="muted">Parsing and validating…</p> : null}

        {record ? (
          <section className="form-section">
            <h4>
              {record.templateName} — {record.namespace ? `${record.namespace} @ ` : ""}
              {record.clusterId || "no cluster"}
            </h4>
            <div className="schema-env-list">
              {checks.length === 0 ? (
                <p className="muted">No validation checks were produced.</p>
              ) : (
                checks.map((c, i) => (
                  <div
                    key={i}
                    className="schema-env-card"
                    style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-start" }}
                  >
                    <span aria-hidden="true">{LEVEL_ICON[c.level] || "•"}</span>
                    <div>
                      <strong>{c.label}</strong>
                      <div className="muted" style={{ fontSize: "0.9em" }}>{c.message}</div>
                    </div>
                  </div>
                ))
              )}
            </div>
            <p className="muted" style={{ marginTop: "var(--space-2)" }}>
              Open the Deploy Wizard to review, finish anything Kubesight fills in (volumes, etc.),
              then deploy, add to a bundle, or send for approval from there.
            </p>
          </section>
        ) : null}

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose}>
            Close
          </button>
          {record ? (
            <>
              <button type="button" className="btn-outline" onClick={handleRevalidate} disabled={busy}>
                {action === "validate" ? "Re-validating…" : "Re-validate"}
              </button>
              <button type="button" className="primary" onClick={handleContinue} disabled={busy}>
                Open in Deploy Wizard
              </button>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
