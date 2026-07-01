import { useEffect, useState } from "react";

import SearchableSelect from "../common/SearchableSelect.jsx";
import NamespaceSelect from "./NamespaceSelect.jsx";
import { normalizeClusterOptions } from "../../utils/clusterOptions.js";
import { generateDeploymentForm } from "../../api/deploymentFormsApi.js";

/**
 * Generate a fillable .xlsx deployment form from a template. Preselecting a
 * cluster (and namespace) lets Kubesight bake live dropdown values (namespaces,
 * ConfigMaps, Secrets, storage classes) into the workbook. The template is always
 * the source of truth — the form only exposes what the template leaves open.
 */
export default function GenerateDeploymentFormModal({
  open,
  template,
  clusterOptions = [],
  defaultClusterId = "",
  onClose,
}) {
  const clusterSelectOptions = normalizeClusterOptions(clusterOptions);
  const [clusterId, setClusterId] = useState(defaultClusterId || "");
  const [namespace, setNamespace] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  useEffect(() => {
    if (open) {
      setClusterId(defaultClusterId || "");
      setNamespace("");
      setBusy(false);
      setError("");
      setDone("");
    }
  }, [open, defaultClusterId]);

  if (!open || !template) return null;

  const handleGenerate = async () => {
    setBusy(true);
    setError("");
    setDone("");
    try {
      const result = await generateDeploymentForm(template.id, {
        clusterId: clusterId || undefined,
        namespace: namespace.trim() || undefined,
      });
      setDone(`Downloaded ${result.filename}. Fill it in and import it from the Deploy Wizard.`);
    } catch (err) {
      setError(err.message || "Failed to generate the deployment form.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>Generate Deployment Form</h3>
          <p className="muted">
            Export <strong>{template.name}</strong> as an Excel form. Locked fields come from the
            template; you only fill the highlighted cells. Choosing a cluster and namespace fills
            in dropdowns (namespaces, ConfigMaps, Secrets, storage classes).
          </p>
        </div>

        {error ? <p className="banner-message error">{error}</p> : null}
        {done ? <p className="banner-message success">{done}</p> : null}

        <section className="form-section">
          <div className="form-grid">
            <label>
              Cluster (optional)
              <SearchableSelect
                value={clusterId}
                onChange={(e) => {
                  setClusterId(e.target.value);
                  setNamespace("");  // namespace-scoped picks reset with the cluster
                }}
              >
                <option value="">— none (dropdowns stay free-text) —</option>
                {clusterSelectOptions.map((c) => (
                  <option key={c.id} value={c.id}>{c.name || c.id}</option>
                ))}
              </SearchableSelect>
            </label>
            <label>
              Namespace (optional)
              <NamespaceSelect
                clusterId={clusterId}
                value={namespace}
                onChange={(e) => setNamespace(e.target.value)}
                required={false}
                allowCreate={false}
              />
            </label>
          </div>
          <p className="muted" style={{ marginTop: "var(--space-2)" }}>
            Pick a namespace to fill the ConfigMap / Secret / TLS dropdowns for that
            namespace. Cluster-only picks (namespaces, storage classes) work without it.
          </p>
        </section>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose}>
            {done ? "Done" : "Cancel"}
          </button>
          <button type="button" className="primary" onClick={handleGenerate} disabled={busy}>
            {busy ? "Generating…" : "Generate & Download"}
          </button>
        </div>
      </div>
    </div>
  );
}
