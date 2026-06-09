import { useEffect, useState } from "react";
import {
  dryRunHelmRelease,
  getHelmConfirmationPhrase,
  installHelmRelease,
  readChartArchiveAsBase64,
  renderHelmTemplate,
} from "../../api/helmApi.js";
import NamespaceSelect from "./NamespaceSelect.jsx";
import { clusterOptionLabel, normalizeClusterOptions } from "../../utils/clusterOptions.js";

const CRITICALITY_OPTIONS = ["Low", "Medium", "High", "Critical"];
const ENVIRONMENT_OPTIONS = ["Development", "Staging", "Production", "DR"];

const EMPTY_HELM = {
  clusterId: "",
  namespace: "",
  releaseName: "",
  chartSource: "repository",
  repositoryName: "",
  repositoryUrl: "",
  chartName: "",
  chartVersion: "latest",
  valuesYaml: "",
  ownerTeam: "",
  environment: "",
  criticality: "",
  description: "",
};

export default function HelmDeployForm({
  clusterOptions,
  defaultClusterId = "",
  onBack,
  onSuccess,
  onCancel,
}) {
  const clusterSelectOptions = normalizeClusterOptions(clusterOptions);
  const resolvedDefaultClusterId =
    defaultClusterId &&
    clusterSelectOptions.some((cluster) => cluster.id === defaultClusterId)
      ? defaultClusterId
      : clusterSelectOptions[0]?.id || "";
  const [step, setStep] = useState("source");
  const [form, setForm] = useState(EMPTY_HELM);
  const [chartFile, setChartFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [confirmation, setConfirmation] = useState("");
  const [confirmationPhrase, setConfirmationPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (resolvedDefaultClusterId && !form.clusterId) {
      setForm((prev) => ({ ...prev, clusterId: resolvedDefaultClusterId }));
    }
  }, [resolvedDefaultClusterId, form.clusterId]);

  useEffect(() => {
    if (step === "confirm" && form.releaseName && form.namespace) {
      getHelmConfirmationPhrase({
        clusterId: form.clusterId,
        namespace: form.namespace,
        releaseName: form.releaseName,
      })
        .then((result) => setConfirmationPhrase(result.confirmation || ""))
        .catch(() => setConfirmationPhrase(""));
    }
  }, [step, form.clusterId, form.namespace, form.releaseName]);

  const buildPayload = async () => {
    const payload = { ...form };
    if (form.chartSource === "local" && chartFile) {
      payload.chartArchiveBase64 = await readChartArchiveAsBase64(chartFile);
    }
    return payload;
  };

  const runPreview = async () => {
    setBusy(true);
    setError("");
    try {
      const payload = await buildPayload();
      const rendered = await renderHelmTemplate(payload);
      const dryRun = await dryRunHelmRelease(payload);
      setPreview({ rendered, dryRun });
      setStep("confirm");
    } catch (err) {
      setError(err.message || "Helm preview failed");
    } finally {
      setBusy(false);
    }
  };

  const applyHelm = async () => {
    setBusy(true);
    setError("");
    try {
      const payload = await buildPayload();
      await installHelmRelease({ ...payload, confirmation });
      onSuccess?.();
    } catch (err) {
      setError(err.message || "Helm install failed");
    } finally {
      setBusy(false);
    }
  };

  if (step === "source") {
    return (
      <div className="add-app-choices">
        <button
          type="button"
          className="btn-outline"
          onClick={() => {
            setForm((p) => ({ ...p, chartSource: "repository" }));
            setStep("form");
          }}
        >
          Helm Repository
        </button>
        <button
          type="button"
          className="btn-outline"
          onClick={() => {
            setForm((p) => ({ ...p, chartSource: "local" }));
            setStep("form");
          }}
        >
          Local .tgz Upload
        </button>
        <button type="button" className="btn-text" onClick={onBack}>Back</button>
      </div>
    );
  }

  if (step === "form") {
    return (
      <form
        className="add-app-form"
        onSubmit={(e) => {
          e.preventDefault();
          runPreview();
        }}
      >
        {error ? <p className="banner-message error">{error}</p> : null}
        <label>
          Cluster
          <select
            required
            value={form.clusterId}
            onChange={(e) => setForm((p) => ({ ...p, clusterId: e.target.value, namespace: "" }))}
          >
            <option value="">Select</option>
            {clusterSelectOptions.map((cluster) => (
              <option key={cluster.id} value={cluster.id}>
                {clusterOptionLabel(cluster)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Namespace
          <NamespaceSelect
            clusterId={form.clusterId}
            value={form.namespace}
            onChange={(e) => setForm((p) => ({ ...p, namespace: e.target.value }))}
          />
        </label>
        <label>Release Name<input required value={form.releaseName} onChange={(e) => setForm((p) => ({ ...p, releaseName: e.target.value.toLowerCase() }))} /></label>
        {form.chartSource === "repository" ? (
          <>
            <label>Repository Name<input required value={form.repositoryName} onChange={(e) => setForm((p) => ({ ...p, repositoryName: e.target.value }))} placeholder="bitnami" /></label>
            <label>Repository URL<input required type="url" value={form.repositoryUrl} onChange={(e) => setForm((p) => ({ ...p, repositoryUrl: e.target.value }))} placeholder="https://charts.bitnami.com/bitnami" /></label>
            <label>Chart Name<input required value={form.chartName} onChange={(e) => setForm((p) => ({ ...p, chartName: e.target.value }))} placeholder="nginx" /></label>
            <label>Chart Version<input value={form.chartVersion} onChange={(e) => setForm((p) => ({ ...p, chartVersion: e.target.value }))} placeholder="latest" /></label>
          </>
        ) : (
          <label>Chart .tgz<input required type="file" accept=".tgz,.tar.gz" onChange={(e) => setChartFile(e.target.files?.[0] || null)} /></label>
        )}
        <label>values.yaml<textarea rows={8} className="yaml-editor" value={form.valuesYaml} onChange={(e) => setForm((p) => ({ ...p, valuesYaml: e.target.value }))} placeholder="# Helm values YAML" /></label>
        <label>Owner / Team<input value={form.ownerTeam} onChange={(e) => setForm((p) => ({ ...p, ownerTeam: e.target.value }))} /></label>
        <label>Environment<select value={form.environment} onChange={(e) => setForm((p) => ({ ...p, environment: e.target.value }))}><option value="">Not set</option>{ENVIRONMENT_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}</select></label>
        <label>Criticality<select value={form.criticality} onChange={(e) => setForm((p) => ({ ...p, criticality: e.target.value }))}><option value="">Not set</option>{CRITICALITY_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}</select></label>
        <label>Description<textarea rows={2} value={form.description} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} /></label>
        <div className="modal-actions">
          <button type="button" className="btn-text" onClick={() => setStep("source")}>Back</button>
          <button type="submit" className="btn-primary" disabled={busy}>{busy ? "Rendering..." : "Render & Dry Run"}</button>
        </div>
      </form>
    );
  }

  return (
    <div className="deploy-preview">
      {error ? <p className="banner-message error">{error}</p> : null}
      <h4>Rendered YAML Preview</h4>
      <pre className="yaml-preview">{preview?.rendered?.preview || preview?.rendered?.rendered || ""}</pre>
      {(preview?.rendered?.warnings || []).length ? (
        <div className="banner-message warn">{(preview.rendered.warnings || []).join("; ")}</div>
      ) : null}
      <h4>Resources</h4>
      <ul>
        {(preview?.dryRun?.resources || preview?.rendered?.resources || []).map((res) => (
          <li key={`${res.kind}-${res.name}`}>{res.kind} {res.name}</li>
        ))}
      </ul>
      <label>
        Type <strong>{confirmationPhrase}</strong> to confirm
        <input value={confirmation} onChange={(e) => setConfirmation(e.target.value)} placeholder={confirmationPhrase} />
      </label>
      <div className="modal-actions">
        <button type="button" className="btn-text" onClick={() => setStep("form")}>Back</button>
        <button type="button" className="btn-primary" disabled={busy || confirmation !== confirmationPhrase} onClick={applyHelm}>
          {busy ? "Installing..." : "Install / Upgrade Release"}
        </button>
      </div>
    </div>
  );
}
