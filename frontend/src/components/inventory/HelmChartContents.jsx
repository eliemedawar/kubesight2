export const HELM_SOURCE_LABELS = {
  yaml: "Deployment YAMLs",
  "git-yaml": "Git · Kubernetes YAML",
  "git-chart": "Git · Helm chart",
  "archive-yaml": "Archive · Kubernetes YAML",
  "archive-chart": "Archive · Helm chart",
};

export function helmSourceLabel(template) {
  return HELM_SOURCE_LABELS[template?.sourceType] || template?.sourceType || "Unknown source";
}

/**
 * Everything KubeSight detected inside a stored chart: Chart.yaml metadata, the
 * rendered templates, and the environment values files that shipped with it.
 * ``compact`` drops the metadata a chart card already shows above it.
 */
export default function HelmChartContents({ template, compact = false }) {
  const chart = template?.chart || {};
  const templates = chart.templates || [];
  const valuesFiles = chart.valuesFiles || [];

  return (
    <div className="helm-chart-contents">
      {compact ? null : (
        <>
          <dl className="helm-chart-contents__meta">
            <div>
              <dt>Chart name</dt>
              <dd>{template?.name || "—"}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>{template?.version || "—"}</dd>
            </div>
            <div>
              <dt>App version</dt>
              <dd>{template?.appVersion || "—"}</dd>
            </div>
            <div>
              <dt>Chart API version</dt>
              <dd>{chart.apiVersion || "—"}</dd>
            </div>
            <div>
              <dt>Chart type</dt>
              <dd>{chart.type || "application"}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd title={template?.sourceRef || ""}>
                {template?.sourceRef || helmSourceLabel(template)}
              </dd>
            </div>
          </dl>

          <p className="helm-chart-contents__description">
            {template?.description || "No chart description was provided."}
          </p>
        </>
      )}

      <div className="helm-chart-contents__lists">
        <section>
          <h5>
            Templates <span className="muted">({templates.length})</span>
          </h5>
          {templates.length ? (
            <ul className="helm-chart-contents__files">
              {templates.map((item) => (
                <li key={item.path}>
                  <code>{item.path}</code>
                  {item.kinds?.length ? (
                    <span className="muted"> · {item.kinds.join(", ")}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No templates/ files were detected in this chart.</p>
          )}
        </section>

        <section>
          <h5>
            Environment values files <span className="muted">({valuesFiles.length})</span>
          </h5>
          {valuesFiles.length ? (
            <>
              <p className="helm-chart-contents__keys muted">
                Stored with the chart for reference. Deployments use the values supplied in the
                deploy form.
              </p>
              <ul className="helm-chart-contents__files">
                {valuesFiles.map((item) => (
                  <li key={item.path}>
                    <code>{item.path}</code>
                    <span className="muted">
                      {" "}
                      · {item.environment} · {item.keyCount} key
                      {item.keyCount === 1 ? "" : "s"}
                    </span>
                    {item.topLevelKeys?.length ? (
                      <p className="helm-chart-contents__keys muted">
                        {item.topLevelKeys.join(", ")}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="muted">
              No values-&lt;environment&gt;.yaml files shipped with this chart.
            </p>
          )}
        </section>
      </div>

      <p className="form-help">
        {chart.hasValuesYaml ? "values.yaml detected" : "No values.yaml in the source"} ·{" "}
        {chart.fileCount || 0} chart file{chart.fileCount === 1 ? "" : "s"} stored
        {compact ? null : (
          <>
            {" "}
            · {template?.variableCount || 0} configurable value
            {template?.variableCount === 1 ? "" : "s"}
            {template?.requiredVariableCount
              ? ` · ${template.requiredVariableCount} required before deploy`
              : ""}
          </>
        )}
      </p>
    </div>
  );
}
