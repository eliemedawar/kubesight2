import { useCallback, useEffect, useState } from "react";

import {
  deleteHelmChartTemplate,
  listHelmChartTemplates,
} from "../../api/helmApi.js";
import HelmChartContents, { helmSourceLabel } from "./HelmChartContents.jsx";
import HelmChartDeployModal from "./HelmChartDeployModal.jsx";
import HelmChartImportModal from "./HelmChartImportModal.jsx";

export default function HelmChartCatalog({
  canDeploy = false,
  canManage = false,
  clusterOptions = [],
  defaultClusterId = "",
  onDeployed,
}) {
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [selectedTemplate, setSelectedTemplate] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await listHelmChartTemplates();
      setTemplates(Array.isArray(result) ? result : []);
    } catch (err) {
      setError(err.message || "Failed to load Helm charts.");
      setTemplates([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const remove = async (template) => {
    if (
      !window.confirm(
        `Delete the reusable Helm chart “${template.name}”? Existing deployed releases are not affected.`,
      )
    ) {
      return;
    }
    setError("");
    try {
      await deleteHelmChartTemplate(template.id);
      await load();
    } catch (err) {
      setError(err.message || "Failed to delete the Helm chart.");
    }
  };

  return (
    <section className="helm-chart-catalog" aria-labelledby="helm-chart-catalog-title">
      <div className="helm-chart-catalog__header">
        <div>
          <h2 id="helm-chart-catalog-title">Helm Charts</h2>
          <p className="muted">
            Reusable charts imported from deployment YAMLs, a chart archive, or Git. Defaults are preserved,
            while sensitive values must be supplied for every deployment.
          </p>
        </div>
        {canManage ? (
          <button type="button" className="btn-primary" onClick={() => setImportOpen(true)}>
            Import Helm Chart
          </button>
        ) : null}
      </div>

      {error ? <p className="banner-message error">{error}</p> : null}
      {loading ? <p className="template-marketplace__empty muted">Loading Helm charts…</p> : null}

      {!loading && !templates.length ? (
        <div className="helm-chart-empty">
          <div className="helm-chart-empty__icon" aria-hidden="true">⌘</div>
          <h3>No reusable Helm charts yet</h3>
          <p className="muted">
            Import several Kubernetes YAMLs, a packaged chart archive, or raw manifests from Git.
          </p>
          {canManage ? (
            <button type="button" className="btn-primary" onClick={() => setImportOpen(true)}>
              Import your first chart
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="helm-chart-grid">
        {templates.map((template) => (
          <article className="card helm-chart-card" key={template.id}>
            <div className="helm-chart-card__top">
              <div className="helm-chart-card__icon" aria-hidden="true">H</div>
              <div>
                <h3>{template.name}</h3>
                <p className="helm-chart-card__version">v{template.version}</p>
              </div>
              {canManage ? (
                <button
                  type="button"
                  className="helm-chart-card__delete"
                  onClick={() => remove(template)}
                  aria-label={`Delete ${template.name}`}
                  title="Delete chart"
                >
                  ×
                </button>
              ) : null}
            </div>
            <p className="helm-chart-card__description">
              {template.description || "Reusable Helm chart"}
            </p>
            <div className="helm-chart-card__meta">
              <span>{helmSourceLabel(template)}</span>
              <span>{template.resourceCount || 0} resources</span>
              <span>{template.variableCount || 0} values</span>
              {template.valuesFileCount ? (
                <span>
                  {template.valuesFileCount} env values file
                  {template.valuesFileCount === 1 ? "" : "s"}
                </span>
              ) : null}
              {template.requiredVariableCount ? (
                <span className="helm-chart-card__required">
                  {template.requiredVariableCount} required
                </span>
              ) : (
                <span className="helm-chart-card__ready">Ready with defaults</span>
              )}
            </div>
            <details className="helm-chart-card__contents">
              <summary>
                Chart contents · {template.templateCount || 0} template
                {template.templateCount === 1 ? "" : "s"}
              </summary>
              <HelmChartContents template={template} compact />
            </details>
            {(template.warnings || []).length ? (
              <details className="helm-chart-card__warnings">
                <summary>{template.warnings.length} import warning{template.warnings.length === 1 ? "" : "s"}</summary>
                <ul>
                  {template.warnings.map((warning, index) => (
                    <li key={`${template.id}-warning-${index}`}>{warning}</li>
                  ))}
                </ul>
              </details>
            ) : null}
            <div className="helm-chart-card__actions">
              <button
                type="button"
                className="btn-primary"
                disabled={!canDeploy}
                onClick={() => setSelectedTemplate(template)}
                title={canDeploy ? "Deploy chart" : "Helm install permission is required"}
              >
                Deploy Chart
              </button>
            </div>
          </article>
        ))}
      </div>

      <HelmChartImportModal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={load}
      />
      <HelmChartDeployModal
        open={Boolean(selectedTemplate)}
        template={selectedTemplate}
        clusterOptions={clusterOptions}
        defaultClusterId={defaultClusterId}
        onClose={() => setSelectedTemplate(null)}
        onSuccess={onDeployed}
      />
    </section>
  );
}
