import { useCallback, useEffect, useState } from "react";

import {
  deleteHelmChartTemplate,
  deleteHelmChartVersion,
  listHelmChartTemplates,
  setHelmChartVersionCurrent,
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
  const [versionTarget, setVersionTarget] = useState(null);
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

  const useVersion = async (template, version) => {
    setError("");
    try {
      await setHelmChartVersionCurrent(template.id, version.version);
      await load();
    } catch (err) {
      setError(err.message || "Failed to switch the chart version.");
    }
  };

  const removeVersion = async (template, version) => {
    if (
      !window.confirm(
        `Delete version ${version.version} of “${template.name}”? Releases already deployed from it are not affected.`,
      )
    ) {
      return;
    }
    setError("");
    try {
      await deleteHelmChartVersion(template.id, version.version);
      await load();
    } catch (err) {
      setError(err.message || "Failed to delete the chart version.");
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
                <p className="helm-chart-card__version">
                  v{template.version}
                  {template.versionCount > 1 ? (
                    <span className="helm-chart-card__version-count">
                      {template.versionCount} versions
                    </span>
                  ) : null}
                </p>
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
            {(template.versions || []).length > 1 ? (
              <details className="helm-chart-card__contents">
                <summary>Versions · {template.versions.length} stored</summary>
                <ul className="helm-chart-version-list">
                  {[...template.versions].reverse().map((version) => (
                    <li
                      key={version.version}
                      className={version.isCurrent ? "is-current" : undefined}
                    >
                      <div className="helm-chart-version-list__info">
                        <span>
                          <code>v{version.version}</code>
                          {version.isCurrent ? (
                            <span className="helm-chart-version-list__badge">current</span>
                          ) : null}
                        </span>
                        <span className="muted">
                          {version.templateCount} template
                          {version.templateCount === 1 ? "" : "s"} ·{" "}
                          {version.valuesFileCount} env file
                          {version.valuesFileCount === 1 ? "" : "s"} ·{" "}
                          {version.requiredVariableCount} required
                        </span>
                        <span className="muted">
                          {version.sourceRef || helmSourceLabel(version)}
                          {version.createdAt
                            ? ` · ${new Date(version.createdAt).toLocaleString()}`
                            : ""}
                        </span>
                      </div>
                      {canManage ? (
                        <div className="helm-chart-version-list__actions">
                          {version.isCurrent ? null : (
                            <button
                              type="button"
                              className="btn-text"
                              onClick={() => useVersion(template, version)}
                            >
                              Make current
                            </button>
                          )}
                          <button
                            type="button"
                            className="helm-chart-card__delete"
                            onClick={() => removeVersion(template, version)}
                            aria-label={`Delete version ${version.version}`}
                            title="Delete version"
                          >
                            ×
                          </button>
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
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
              {canManage ? (
                <button
                  type="button"
                  className="btn-text"
                  onClick={() => setVersionTarget(template)}
                  title="Upload another packaged chart as a new version"
                >
                  Add Version
                </button>
              ) : null}
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
      <HelmChartImportModal
        open={Boolean(versionTarget)}
        targetTemplate={versionTarget}
        onClose={() => setVersionTarget(null)}
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
