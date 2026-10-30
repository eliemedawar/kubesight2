import { useEffect, useMemo, useRef, useState } from "react";

import {
  dryRunHelmRelease,
  getHelmChartTemplate,
  getHelmConfirmationPhrase,
  getHelmUpgradeStart,
  inspectHelmValuesImport,
  installHelmRelease,
  renderHelmTemplate,
  upgradeHelmRelease,
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

// Form values from field values the server worked out (an upgrade's start).
function formValuesFrom(detail, fields) {
  return Object.fromEntries(
    (detail?.variables || []).map((variable) => [
      variable.path,
      initialFieldValue({ ...variable, default: fields?.[variable.path] }),
    ]),
  );
}

// An upgrade sends only the fields the person changed: everything else keeps
// the release's current value, which the server layers in.
function changedValues(values, start) {
  return Object.fromEntries(
    Object.entries(values).filter(([path, value]) => String(value ?? "") !== String(start?.[path] ?? "")),
  );
}

function VariableField({ variable, value, fromFile, keptSecret, onChange }) {
  const id = `helm-variable-${variable.key}`;
  // A password the release already holds is kept unless typed over.
  const required = Boolean(variable.required) && !keptSecret;
  const common = {
    id,
    value,
    required,
    onChange: (event) => onChange(event.target.value),
  };
  return (
    <label htmlFor={id} className={required ? "helm-variable-required" : ""}>
      <span>
        {variable.label || variable.path}
        {required ? <strong aria-label="required"> *</strong> : null}
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
          placeholder={keptSecret ? "Kept from the release. Type to replace it." : undefined}
          autoComplete={variable.sensitive ? "new-password" : undefined}
        />
      )}
      {variable.description ? <small className="form-help">{variable.description}</small> : null}
    </label>
  );
}

/**
 * Deploy a stored chart, or — with ``release`` ({ clusterId, namespace,
 * releaseName, chart, revision }) — upgrade that installed release onto a
 * stored chart version. An upgrade starts from the release's current values;
 * its passwords stay on the server and only changed fields are sent.
 */
export default function HelmChartDeployModal({
  open,
  template,
  release = null,
  chartOptions = [],
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
  // The stored chart: the one clicked, or for an upgrade the one picked.
  const [chartId, setChartId] = useState(template?.id || "");
  // An upgrade's starting point: { form, keptSecrets, missingRequired, valuesShown }.
  const [upgradeStart, setUpgradeStart] = useState(null);
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

  const releaseTarget = release
    ? { clusterId: release.clusterId, namespace: release.namespace, releaseName: release.releaseName }
    : null;

  useEffect(() => {
    if (open) setChartId(template?.id || "");
  }, [open, template?.id]);

  // The form before any import: the chart's values, or for an upgrade the
  // release's current values over the chart's (worked out on the server).
  const loadBase = async (chartDetail, environmentFile) => {
    if (!releaseTarget) return initialValues(chartDetail, environmentFile);
    const start = await getHelmUpgradeStart({
      ...releaseTarget,
      chartTemplateId: chartDetail.id,
      version: chartDetail.version,
      valuesFile: environmentFile,
    });
    const form = formValuesFrom(chartDetail, start.fields);
    setUpgradeStart({ ...start, form });
    return form;
  };

  // Check the file against this chart version and environment file, then fill
  // every field it touches over ``base``. Throws when the file is not a values
  // mapping.
  const applyImport = async (text, name, chartDetail, environmentFile, base) => {
    const report = await inspectHelmValuesImport(chartDetail.id, {
      valuesYaml: text,
      version: chartDetail.version,
      valuesFile: environmentFile,
      ...(releaseTarget ? { ...releaseTarget, keepCurrentValues: true } : {}),
    });
    setImported({ text, name, report });
    setValues({ ...base, ...importedFieldValues(chartDetail, report) });
  };

  // Refill the form for a chart version / environment file, re-applying an
  // imported values.yaml on top.
  const refill = async (chartDetail, environmentFile) => {
    const base = await loadBase(chartDetail, environmentFile);
    setValues(base);
    const previous = importedRef.current;
    if (!previous) return;
    try {
      await applyImport(previous.text, previous.name, chartDetail, environmentFile, base);
    } catch (err) {
      setImported(null);
      setError(err.message || "The imported values.yaml no longer fits this chart version.");
    }
  };

  useEffect(() => {
    if (!open || !chartId) return;
    let cancelled = false;
    setBusy(true);
    setError("");
    // An empty version asks the backend for whichever version is current.
    getHelmChartTemplate(chartId, version)
      .then((result) => {
        if (cancelled) return undefined;
        setDetail(result);
        setReleaseName((previous) =>
          release?.releaseName ||
          previous ||
          String(result.id || "release")
            .toLowerCase()
            .replace(/[^a-z0-9-]+/g, "-")
            .replace(/^-+|-+$/g, "")
            .slice(0, 53),
        );
        setValuesFile("");
        return refill(result, "");
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
  }, [open, chartId, version]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) {
      setDetail(null);
      setUpgradeStart(null);
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

  const keptSecrets = useMemo(() => new Set(upgradeStart?.keptSecrets || []), [upgradeStart]);
  // Kept passwords stay in this list though no longer required, so replacing
  // one is as easy to find as typing it was.
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
    chartTemplateId: detail?.id || chartId,
    chartVersion: version || detail?.version || "",
    clusterId,
    namespace,
    releaseName,
    valuesFile,
    values,
    valuesYaml: imported?.text || "",
    ...(releaseTarget
      ? {
          ...releaseTarget,
          isUpgrade: true,
          keepCurrentValues: true,
          values: changedValues(values, upgradeStart?.form),
        }
      : {}),
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
      const send = releaseTarget ? upgradeHelmRelease : installHelmRelease;
      const result = await send({ ...payload, confirmation });
      if (isPendingApproval(result)) {
        // Nothing is installed yet, so no success callback.
        setQueuedMessage(pendingApprovalMessage(result));
        return;
      }
      onSuccess?.();
      onClose();
    } catch (err) {
      setError(err.message || (releaseTarget ? "Helm upgrade failed." : "Helm installation failed."));
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
            <h3 id="helm-deploy-title">
              {release ? `Upgrade ${release.releaseName}` : `Deploy ${detail?.name || template?.name}`}
            </h3>
            <p className="muted">
              {step !== "configure"
                ? `Review the rendered resources before ${release ? "upgrading" : "installation"}.`
                : release
                  ? "Pick the stored chart version to move to. The release keeps its current values; change only what should change."
                  : "Choose a target and review only the values this chart exposes."}
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
            {release ? (
              <dl className="helm-upgrade-target">
                <div>
                  <dt>Installed</dt>
                  <dd>
                    {release.chart || "unknown chart"}
                    {release.revision ? <span className="muted"> · revision {release.revision}</span> : null}
                  </dd>
                </div>
                <div>
                  <dt>Where</dt>
                  <dd>
                    {release.namespace} <span className="muted">on</span>{" "}
                    {release.clusterLabel || release.clusterId}
                  </dd>
                </div>
              </dl>
            ) : null}

            {release ? (
              <label className="helm-version-picker">
                <span>Upgrade to chart</span>
                <SearchableSelect
                  required
                  value={chartId}
                  onChange={(event) => {
                    setImported(null);
                    setDetail(null);
                    setUpgradeStart(null);
                    setVersion("");
                    setChartId(event.target.value);
                  }}
                >
                  <option value="">Pick a stored chart</option>
                  {chartOptions.map((chart) => (
                    <option key={chart.id} value={chart.id}>
                      {chart.name}
                    </option>
                  ))}
                </SearchableSelect>
                {!chartId ? (
                  <small className="form-help">
                    No stored chart matches {release.chart || "this release"}. Pick the one to
                    upgrade to, or import it under Helm Charts first.
                  </small>
                ) : null}
              </label>
            ) : null}

            {versions.length > 1 || (release && versions.length) ? (
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
                    setBusy(true);
                    setError("");
                    try {
                      await refill(detail, selected);
                    } catch (err) {
                      setError(err.message || "The values could not be loaded.");
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
                  {release
                    ? "Applied under the release's current values: it only adds what the release never set."
                    : "The selected file is applied first. Any value changed below overrides it."}
                </small>
              </label>
            ) : null}

            {release ? null : (
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
            )}

            {release && upgradeStart && !upgradeStart.valuesShown ? (
              <p className="banner-message" role="note">
                You can&apos;t view this release&apos;s values, so the fields show the chart&apos;s.
                Anything you leave alone keeps the release&apos;s current value.
              </p>
            ) : null}

            {busy && !detail ? <p className="muted">Loading chart values…</p> : null}

            {detail ? (
              <HelmValuesImport
                templateId={detail.id}
                version={detail.version}
                imported={imported}
                values={values}
                busy={busy}
                canSave={canManage}
                layeredOver={release ? "the release's current values" : ""}
                onApply={async (text, name) => {
                  setBusy(true);
                  setError("");
                  try {
                    const base = release ? upgradeStart?.form || {} : initialValues(detail, valuesFile);
                    await applyImport(text, name, detail, valuesFile, base);
                  } finally {
                    setBusy(false);
                  }
                }}
                onClear={() => {
                  setImported(null);
                  setValues(release ? upgradeStart?.form || {} : initialValues(detail, valuesFile));
                }}
                // The saved file joins Environment values; the selection stays.
                onSaved={(result) => setDetail(result)}
              />
            ) : null}

            {requiredVariables.length ? (
              <section className="helm-variable-section">
                <h4>{keptSecrets.size ? "Passwords and required values" : "Required values"}</h4>
                <div className="helm-variable-grid">
                  {requiredVariables.map((variable) => (
                    <VariableField
                      key={variable.path}
                      variable={variable}
                      value={values[variable.path] ?? ""}
                      fromFile={Boolean(imported && Object.hasOwn(imported.report.fields || {}, variable.path))}
                      keptSecret={keptSecrets.has(variable.path) && !String(values[variable.path] ?? "")}
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
                      keptSecret={keptSecrets.has(variable.path) && !String(values[variable.path] ?? "")}
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
                  {busy
                    ? release ? "Upgrading…" : "Installing…"
                    : release ? "Upgrade Release" : "Install Release"}
                </button>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
