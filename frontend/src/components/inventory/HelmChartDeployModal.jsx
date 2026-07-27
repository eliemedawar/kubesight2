import { useEffect, useMemo, useState } from "react";

import {
  dryRunHelmRelease,
  getHelmChartTemplate,
  getHelmConfirmationPhrase,
  installHelmRelease,
  renderHelmTemplate,
} from "../../api/helmApi.js";
import { clusterOptionLabel, normalizeClusterOptions } from "../../utils/clusterOptions.js";
import SearchableSelect from "../common/SearchableSelect.jsx";
import NamespaceSelect from "./NamespaceSelect.jsx";

function initialFieldValue(variable) {
  const value = variable?.default;
  if (variable?.type === "array" || variable?.type === "object") {
    return value == null ? "" : JSON.stringify(value, null, 2);
  }
  return value ?? "";
}

function VariableField({ variable, value, onChange }) {
  const id = `helm-variable-${variable.key}`;
  const common = {
    id,
    value,
    required: Boolean(variable.required),
    onChange: (event) => onChange(event.target.value),
  };
  return (
    <label htmlFor={id} className={variable.required ? "helm-variable-required" : ""}>
      <span>
        {variable.label || variable.path}
        {variable.required ? <strong aria-label="required"> *</strong> : null}
      </span>
      {variable.type === "boolean" ? (
        <SearchableSelect {...common}>
          <option value="true">True</option>
          <option value="false">False</option>
        </SearchableSelect>
      ) : variable.type === "array" || variable.type === "object" ? (
        <textarea {...common} rows={4} className="yaml-editor" />
      ) : (
        <input
          {...common}
          type={
            variable.sensitive
              ? "password"
              : variable.type === "integer" || variable.type === "number"
                ? "number"
                : "text"
          }
          step={variable.type === "number" ? "any" : undefined}
          autoComplete={variable.sensitive ? "new-password" : undefined}
        />
      )}
      {variable.description ? <small className="form-help">{variable.description}</small> : null}
    </label>
  );
}

export default function HelmChartDeployModal({
  open,
  template,
  clusterOptions = [],
  defaultClusterId = "",
  onClose,
  onSuccess,
}) {
  const options = normalizeClusterOptions(clusterOptions);
  const resolvedCluster =
    defaultClusterId && options.some((item) => item.id === defaultClusterId)
      ? defaultClusterId
      : options[0]?.id || "";
  const [detail, setDetail] = useState(null);
  const [clusterId, setClusterId] = useState(resolvedCluster);
  const [namespace, setNamespace] = useState("");
  const [releaseName, setReleaseName] = useState("");
  const [values, setValues] = useState({});
  const [step, setStep] = useState("configure");
  const [preview, setPreview] = useState(null);
  const [confirmationPhrase, setConfirmationPhrase] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || !template?.id) return;
    let cancelled = false;
    setBusy(true);
    setError("");
    getHelmChartTemplate(template.id)
      .then((result) => {
        if (cancelled) return;
        setDetail(result);
        setReleaseName(
          String(result.id || "release")
            .toLowerCase()
            .replace(/[^a-z0-9-]+/g, "-")
            .replace(/^-+|-+$/g, "")
            .slice(0, 53),
        );
        setValues(
          Object.fromEntries(
            (result.variables || []).map((variable) => [
              variable.path,
              initialFieldValue(variable),
            ]),
          ),
        );
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || "Failed to load the Helm chart.");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, template?.id]);

  useEffect(() => {
    if (!open) {
      setDetail(null);
      setClusterId(resolvedCluster);
      setNamespace("");
      setReleaseName("");
      setValues({});
      setStep("configure");
      setPreview(null);
      setConfirmationPhrase("");
      setConfirmation("");
      setError("");
      setBusy(false);
    }
  }, [open, resolvedCluster]);

  const requiredVariables = useMemo(
    () => (detail?.variables || []).filter((variable) => variable.required),
    [detail],
  );
  const optionalVariables = useMemo(
    () => (detail?.variables || []).filter((variable) => !variable.required),
    [detail],
  );

  if (!open) return null;

  const payload = {
    chartSource: "template",
    chartTemplateId: detail?.id || template?.id,
    clusterId,
    namespace,
    releaseName,
    values,
  };

  const previewChart = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const rendered = await renderHelmTemplate(payload);
      const dryRun = await dryRunHelmRelease(payload);
      const confirmationResult = await getHelmConfirmationPhrase(payload);
      setPreview({ rendered, dryRun });
      setConfirmationPhrase(confirmationResult.confirmation || "");
      setConfirmation("");
      setStep("preview");
    } catch (err) {
      setError(err.message || "Helm preview failed.");
    } finally {
      setBusy(false);
    }
  };

  const install = async () => {
    setBusy(true);
    setError("");
    try {
      await installHelmRelease({ ...payload, confirmation });
      onSuccess?.();
      onClose();
    } catch (err) {
      setError(err.message || "Helm installation failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel helm-chart-deploy-modal"
        role="dialog"
        aria-labelledby="helm-deploy-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3 id="helm-deploy-title">Deploy {detail?.name || template?.name}</h3>
            <p className="muted">
              {step === "configure"
                ? "Choose a target and review only the values this chart exposes."
                : "Review the rendered resources before installation."}
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        {error ? <p className="banner-message error">{error}</p> : null}

        {step === "configure" ? (
          <form className="add-app-form" onSubmit={previewChart}>
            <div className="helm-target-grid">
              <label>
                Cluster
                <SearchableSelect
                  required
                  value={clusterId}
                  onChange={(event) => {
                    setClusterId(event.target.value);
                    setNamespace("");
                  }}
                >
                  <option value="">Select cluster</option>
                  {options.map((cluster) => (
                    <option key={cluster.id} value={cluster.id}>
                      {clusterOptionLabel(cluster)}
                    </option>
                  ))}
                </SearchableSelect>
              </label>
              <label>
                Namespace
                <NamespaceSelect
                  required
                  clusterId={clusterId}
                  value={namespace}
                  onChange={(event) => setNamespace(event.target.value)}
                />
              </label>
              <label>
                Release name
                <input
                  required
                  maxLength={53}
                  pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?"
                  value={releaseName}
                  onChange={(event) =>
                    setReleaseName(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))
                  }
                />
              </label>
            </div>

            {busy && !detail ? <p className="muted">Loading chart values…</p> : null}

            {requiredVariables.length ? (
              <section className="helm-variable-section">
                <h4>Required values</h4>
                <div className="helm-variable-grid">
                  {requiredVariables.map((variable) => (
                    <VariableField
                      key={variable.path}
                      variable={variable}
                      value={values[variable.path] ?? ""}
                      onChange={(value) =>
                        setValues((previous) => ({ ...previous, [variable.path]: value }))
                      }
                    />
                  ))}
                </div>
              </section>
            ) : (
              <p className="helm-ready-message">
                This chart has safe defaults and does not require any additional values.
              </p>
            )}

            {optionalVariables.length ? (
              <details className="helm-optional-values">
                <summary>
                  Review optional values <span>({optionalVariables.length})</span>
                </summary>
                <div className="helm-variable-grid">
                  {optionalVariables.map((variable) => (
                    <VariableField
                      key={variable.path}
                      variable={variable}
                      value={values[variable.path] ?? ""}
                      onChange={(value) =>
                        setValues((previous) => ({ ...previous, [variable.path]: value }))
                      }
                    />
                  ))}
                </div>
              </details>
            ) : null}

            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button type="submit" className="btn-primary" disabled={busy || !detail}>
                {busy ? "Rendering…" : "Render & Dry Run"}
              </button>
            </div>
          </form>
        ) : (
          <div className="deploy-preview">
            <h4>Resources</h4>
            <ul className="helm-resource-list">
              {(preview?.dryRun?.resources || preview?.rendered?.resources || []).map(
                (resource) => (
                  <li key={`${resource.kind}-${resource.namespace || ""}-${resource.name}`}>
                    <strong>{resource.kind}</strong> {resource.name}
                  </li>
                ),
              )}
            </ul>
            <details className="helm-rendered-preview">
              <summary>Rendered YAML</summary>
              <pre className="yaml-preview">
                {preview?.rendered?.preview || preview?.rendered?.rendered || ""}
              </pre>
            </details>
            {(preview?.rendered?.warnings || []).length ? (
              <div className="banner-message warn">
                {(preview.rendered.warnings || []).join("; ")}
              </div>
            ) : null}
            <label>
              Type <strong>{confirmationPhrase}</strong> to confirm
              <input
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                placeholder={confirmationPhrase}
              />
            </label>
            <div className="modal-actions">
              <button type="button" className="btn-text" onClick={() => setStep("configure")}>
                Back
              </button>
              <button
                type="button"
                className="btn-primary"
                disabled={busy || confirmation !== confirmationPhrase}
                onClick={install}
              >
                {busy ? "Installing…" : "Install Release"}
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
