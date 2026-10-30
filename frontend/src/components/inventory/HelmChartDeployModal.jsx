import { useEffect, useMemo, useRef, useState } from "react";

import {
  dryRunHelmRelease,
  getHelmChartTemplate,
  getHelmConfirmationPhrase,
  inspectHelmValuesImport,
  installHelmRelease,
  renderHelmTemplate,
} from "../../api/helmApi.js";
import { clusterOptionLabel, normalizeClusterOptions } from "../../utils/clusterOptions.js";
import SearchableSelect from "../common/SearchableSelect.jsx";
import NamespaceSelect from "./NamespaceSelect.jsx";
import HelmValuesImport from "./HelmValuesImport.jsx";
import useEscapeKey from "../../hooks/useEscapeKey.js";
import { isPendingApproval, pendingApprovalMessage } from "../../utils/pendingApproval.js";

function initialFieldValue(variable) {
  const value = variable?.default;
  if (value == null || value === "") {
    // A blanked default (e.g. a scrubbed secret) stays an empty field rather
    // than rendering as a literal `""` for the operator to delete.
    return "";
  }
  if (variable?.type === "array" || variable?.type === "object") {
    return JSON.stringify(value, null, 2);
  }
  return value;
}

function valueAtPath(values, path) {
  let current = values;
  for (const part of String(path || "").split(".")) {
    if (!current || typeof current !== "object" || !(part in current)) {
      return { found: false, value: undefined };
    }
    current = current[part];
  }
  return { found: true, value: current };
}

function initialValues(detail, valuesFile = "") {
  const selected = (detail?.chart?.valuesFiles || []).find((item) => item.path === valuesFile);
  return Object.fromEntries(
    (detail?.variables || []).map((variable) => {
      const environmentValue = valueAtPath(selected?.values, variable.path);
      return [
        variable.path,
        initialFieldValue(
          environmentValue.found ? { ...variable, default: environmentValue.value } : variable,
        ),
      ];
    }),
  );
}

// The form value of each field an imported values.yaml touches.
function importedFieldValues(detail, report) {
  const fields = report?.fields || {};
  return Object.fromEntries(
    (detail?.variables || [])
      .filter((variable) => Object.hasOwn(fields, variable.path))
      .map((variable) => [
        variable.path,
        initialFieldValue({ ...variable, default: fields[variable.path] }),
      ]),
  );
}

function VariableField({ variable, value, fromFile, onChange }) {
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
        {fromFile ? <em className="helm-variable-from-file">from file</em> : null}
      </span>
      {variable.type === "boolean" ? (
        <SearchableSelect {...common}>
          <option value="true">True</option>
          <option value="false">False</option>
        </SearchableSelect>
      ) : variable.type === "array" || variable.type === "object" ? (
        <textarea
          {...common}
          rows={4}
          className="yaml-editor"
          placeholder={variable.type === "array" ? "- first-item" : "key: value"}
          spellCheck={false}
        />
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
  canManage = false,
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
  const [version, setVersion] = useState("");
  const [clusterId, setClusterId] = useState(resolvedCluster);
  const [namespace, setNamespace] = useState("");
  const [releaseName, setReleaseName] = useState("");
  const [valuesFile, setValuesFile] = useState("");
  const [values, setValues] = useState({});
  // An imported values.yaml: { name, text, report }. Sent whole as valuesYaml.
  const [imported, setImported] = useState(null);
  const importedRef = useRef(null);
  importedRef.current = imported;
  const [step, setStep] = useState("configure");
  const [preview, setPreview] = useState(null);
  const [confirmationPhrase, setConfirmationPhrase] = useState("");
  const [confirmation, setConfirmation] = useState("");
  // Set when the release was sent for approval instead of installed.
  const [queuedMessage, setQueuedMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Check the file against this chart version and environment file, then fill
  // every field it touches. Throws when the file is not a values mapping.
  const applyImport = async (text, name, chartDetail, environmentFile) => {
    const report = await inspectHelmValuesImport(chartDetail.id, {
      valuesYaml: text,
      version: chartDetail.version,
      valuesFile: environmentFile,
    });
    setImported({ text, name, report });
    setValues({
      ...initialValues(chartDetail, environmentFile),
      ...importedFieldValues(chartDetail, report),
    });
  };

  useEffect(() => {
    if (!open || !template?.id) return;
    let cancelled = false;
    setBusy(true);
    setError("");
    // An empty version asks the backend for whichever version is current.
    getHelmChartTemplate(template.id, version)
      .then((result) => {
        if (cancelled) return;
        setDetail(result);
        setReleaseName((previous) =>
          previous ||
          String(result.id || "release")
            .toLowerCase()
            .replace(/[^a-z0-9-]+/g, "-")
            .replace(/^-+|-+$/g, "")
            .slice(0, 53),
        );
        setValuesFile("");
        setValues(initialValues(result));
        // Another version: the same file fills that version's fields.
        const previous = importedRef.current;
        if (previous) {
          return applyImport(previous.text, previous.name, result, "").catch((err) => {
            if (cancelled) return;
            setImported(null);
            setError(err.message || "The imported values.yaml no longer fits this version.");
          });
        }
        return undefined;
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
  }, [open, template?.id, version]);

  useEffect(() => {
    if (!open) {
      setDetail(null);
      setVersion("");
      setClusterId(resolvedCluster);
      setNamespace("");
      setReleaseName("");
      setValuesFile("");
      setValues({});
      setImported(null);
      setStep("configure");
      setPreview(null);
      setConfirmationPhrase("");
      setConfirmation("");
      // The modal stays mounted between charts: a previous "sent for approval"
      // must not keep Install disabled for the next one.
      setQueuedMessage("");
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

  useEscapeKey(open, onClose, busy);

  if (!open) return null;

  const versions = detail?.versions || [];
  const payload = {
    chartSource: "template",
    chartTemplateId: detail?.id || template?.id,
    chartVersion: version || detail?.version || "",
    clusterId,
    namespace,
    releaseName,
    valuesFile,
    values,
    valuesYaml: imported?.text || "",
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
      const result = await installHelmRelease({ ...payload, confirmation });
      if (isPendingApproval(result)) {
        // Nothing is installed yet, so no success callback.
        setQueuedMessage(pendingApprovalMessage(result));
        return;
      }
      onSuccess?.();
      onClose();
    } catch (err) {
      setError(err.message || "Helm installation failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={busy ? undefined : onClose}>
      <section
        className="card modal-panel helm-chart-deploy-modal"
        role="dialog"
        aria-modal="true"
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
        {queuedMessage ? <p className="banner-message" role="status">{queuedMessage}</p> : null}

        {step === "configure" ? (
          <form className="add-app-form" onSubmit={previewChart}>
            {versions.length > 1 ? (
              <label className="helm-version-picker">
                <span>
                  Chart version <span className="muted">({versions.length} stored)</span>
                </span>
                <SearchableSelect
                  value={version || detail?.version || ""}
                  onChange={(event) => {
                    setValuesFile("");
                    setVersion(event.target.value);
                  }}
                >
                  {versions.map((item) => (
                    <option key={item.version} value={item.version}>
                      v{item.version}
                      {item.isCurrent ? " (current)" : ""}
                      {item.appVersion ? ` · app ${item.appVersion}` : ""}
                    </option>
                  ))}
                </SearchableSelect>
              </label>
            ) : null}

            {(detail?.chart?.valuesFiles || []).length ? (
              <label className="helm-version-picker">
                <span>Environment values</span>
                <SearchableSelect
                  value={valuesFile}
                  onChange={async (event) => {
                    const selected = event.target.value;
                    setValuesFile(selected);
                    if (!imported) {
                      setValues(initialValues(detail, selected));
                      return;
                    }
                    setBusy(true);
                    setError("");
                    try {
                      await applyImport(imported.text, imported.name, detail, selected);
                    } catch (err) {
                      setError(err.message || "The imported values.yaml could not be applied.");
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <option value="">Base values.yaml</option>
                  {(detail.chart.valuesFiles || []).map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.environment} · {item.path}
                    </option>
                  ))}
                </SearchableSelect>
                <small className="form-help">
                  The selected file is applied first. Any value changed below overrides it.
                </small>
              </label>
            ) : null}

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
                  pattern="[a-z0-9]([a-z0-9\-]*[a-z0-9])?"
                  value={releaseName}
                  onChange={(event) =>
                    setReleaseName(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))
                  }
                />
              </label>
            </div>

            {busy && !detail ? <p className="muted">Loading chart values…</p> : null}

            {detail ? (
              <HelmValuesImport
                templateId={detail.id}
                version={detail.version}
                imported={imported}
                values={values}
                busy={busy}
                canSave={canManage}
                onApply={async (text, name) => {
                  setBusy(true);
                  setError("");
                  try {
                    await applyImport(text, name, detail, valuesFile);
                  } finally {
                    setBusy(false);
                  }
                }}
                onClear={() => {
                  setImported(null);
                  setValues(initialValues(detail, valuesFile));
                }}
                // The saved file joins Environment values; the selection stays.
                onSaved={(result) => setDetail(result)}
              />
            ) : null}

            {requiredVariables.length ? (
              <section className="helm-variable-section">
                <h4>Required values</h4>
                <div className="helm-variable-grid">
                  {requiredVariables.map((variable) => (
                    <VariableField
                      key={variable.path}
                      variable={variable}
                      value={values[variable.path] ?? ""}
                      fromFile={Boolean(imported && Object.hasOwn(imported.report.fields || {}, variable.path))}
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
                      fromFile={Boolean(imported && Object.hasOwn(imported.report.fields || {}, variable.path))}
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
              <span>
                Type <strong>{confirmationPhrase}</strong> to confirm
              </span>
              <input
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                placeholder={confirmationPhrase}
              />
            </label>
            <div className="modal-actions">
              <button
                type="button"
                className="btn-text"
                onClick={() => setStep("configure")}
                disabled={busy || Boolean(queuedMessage)}
              >
                Back
              </button>
              {queuedMessage ? (
                // Sent for approval: nothing left to do here.
                <button type="button" className="btn-primary" onClick={onClose}>
                  Done
                </button>
              ) : (
                <button
                  type="button"
                  className="btn-primary"
                  disabled={busy || confirmation !== confirmationPhrase}
                  onClick={install}
                >
                  {busy ? "Installing…" : "Install Release"}
                </button>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
