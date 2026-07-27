import { useMemo, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ZohoLayoutEditor from "./ZohoLayoutEditor.jsx";

const AGENT_LIST_CAP = 6;

// Simulated Zoho Desk ticket form: pick an Environment, watch the Application
// list narrow exactly the way the cascade narrows it for the agent.
function AgentPreview({ preview, previewLoading, cascadeOn }) {
  const namespaces = preview?.namespaces || [];
  const items = preview?.items || [];
  const manageVariables = Boolean(preview?.manageVariables);
  const [env, setEnv] = useState("");
  const [app, setApp] = useState("");
  const activeEnv = env || namespaces[0] || "";

  const apps = useMemo(() => {
    const source = cascadeOn && activeEnv ? items.filter((r) => r.namespace === activeEnv) : items;
    return [...new Set(source.map((r) => r.label))].sort();
  }, [items, activeEnv, cascadeOn]);

  const activeApp = apps.includes(app) ? app : apps[0] || "";

  // Variable options for the picked app — the App→Variable cascade preview.
  const variables = useMemo(() => {
    if (!manageVariables || !activeApp) return [];
    const source = items.filter(
      (r) => r.label === activeApp && (!cascadeOn || !activeEnv || r.namespace === activeEnv)
    );
    return [...new Set(source.flatMap((r) => r.variables || []))].sort();
  }, [items, activeApp, activeEnv, cascadeOn, manageVariables]);

  return (
    <section className="card">
      <div className="card-header-row">
        <h3>What the agent sees</h3>
        <span className="sg-zh-count">live preview</span>
      </div>
      {previewLoading ? (
        <p className="muted">Reading the live deployments from the source cluster…</p>
      ) : items.length === 0 ? (
        <EmptyState message="Nothing published yet — pick a cluster and namespaces first." />
      ) : (
        <div className="sg-zh-zform">
          <div className="sg-zh-zform-title">Zoho Desk · New DevOps Request</div>
          <label className="sg-zh-zfield">
            Environment
            <select value={activeEnv} onChange={(e) => setEnv(e.target.value)}>
              {namespaces.map((ns) => (
                <option key={ns} value={ns}>
                  {ns}
                </option>
              ))}
            </select>
          </label>
          {manageVariables ? (
            <label className="sg-zh-zfield">
              Application
              <select value={activeApp} onChange={(e) => setApp(e.target.value)}>
                {apps.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div className="sg-zh-zfield">
              <span className="sg-zh-zfield-label">Application</span>
              <div className="sg-zh-zoptions" role="listbox" aria-label="Application options">
                {apps.slice(0, AGENT_LIST_CAP).map((app) => (
                  <span key={app} className="sg-zh-zopt mono">
                    {app}
                  </span>
                ))}
                {apps.length > AGENT_LIST_CAP ? (
                  <span className="sg-zh-zopt sg-zh-zopt--more">
                    +{apps.length - AGENT_LIST_CAP} more
                  </span>
                ) : null}
                {apps.length === 0 ? (
                  <span className="sg-zh-zopt sg-zh-zopt--more">no applications here</span>
                ) : null}
              </div>
            </div>
          )}
          {manageVariables ? (
            <div className="sg-zh-zfield">
              <span className="sg-zh-zfield-label">Variable</span>
              <div className="sg-zh-zoptions" role="listbox" aria-label="Variable options">
                {variables.slice(0, AGENT_LIST_CAP).map((v) => (
                  <span key={v} className="sg-zh-zopt mono">
                    {v}
                  </span>
                ))}
                {variables.length > AGENT_LIST_CAP ? (
                  <span className="sg-zh-zopt sg-zh-zopt--more">
                    +{variables.length - AGENT_LIST_CAP} more
                  </span>
                ) : null}
                {variables.length === 0 ? (
                  <span className="sg-zh-zopt sg-zh-zopt--more">no changeable variables</span>
                ) : null}
              </div>
            </div>
          ) : null}
          <p className="sg-zh-zcaption">
            {cascadeOn ? (
              <>
                <b>Cascade in action:</b> choosing <span className="mono">{activeEnv}</span> narrows
                Application to the {apps.length} app(s) deployed there
                {manageVariables && activeApp ? (
                  <>
                    ; picking <span className="mono">{activeApp}</span> narrows Variable to its{" "}
                    {variables.length} changeable env var(s)
                  </>
                ) : null}
                . A ticket resolves by Application + Environment.
              </>
            ) : (
              <>
                Cascade is off — agents see all {apps.length} applications regardless of the
                selected Environment.
              </>
            )}
          </p>
        </div>
      )}
    </section>
  );
}

function PublishedValues({ preview, previewLoading }) {
  const [filter, setFilter] = useState("");
  const items = preview?.items || [];
  const query = filter.trim().toLowerCase();
  const filtered = query
    ? items.filter(
        (row) =>
          (row.label || "").toLowerCase().includes(query) ||
          (row.deploymentName || "").toLowerCase().includes(query) ||
          (row.namespace || "").toLowerCase().includes(query)
      )
    : items;

  return (
    <section className="card">
      <div className="card-header-row">
        <h3>Published values</h3>
        <div className="sg-zh-head-tools">
          {!previewLoading && items.length > 0 ? (
            <input
              type="search"
              className="sg-zh-filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder={`Filter ${items.length} rows…`}
              aria-label="Filter deployments"
            />
          ) : null}
          <span className="sg-zh-count">
            {previewLoading
              ? "…"
              : query
              ? `${filtered.length} of ${items.length}`
              : `${preview?.count ?? 0} unique`}
          </span>
        </div>
      </div>
      <p className="muted">
        Every name/namespace pairing published into the Zoho <code>Application</code> field
        {preview?.sourceClusterId ? (
          <>
            {" "}
            from cluster <code>{preview.sourceClusterId}</code>
          </>
        ) : null}
        . Names shared across namespaces appear once in Zoho.
      </p>
      {previewLoading ? (
        <p className="muted">Loading…</p>
      ) : preview?.error ? (
        <p className="sg-zh-inline-error">{preview.error}</p>
      ) : items.length === 0 ? (
        <EmptyState message="No deployments yet — pick a cluster and namespaces via “Choose namespaces”." />
      ) : (
        <div className="table-wrap sg-zh-tscroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Application value</th>
                <th>Deployment</th>
                <th>Namespace</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.label}</td>
                  <td className="mono">{row.deploymentName}</td>
                  <td>
                    <span className="sg-tag">{row.namespace}</span>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={3} className="muted">
                    No rows match “{filter}”.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// Outbound room: the layout editor beside the preview it feeds.
export default function ZohoFieldSyncTab({
  canManage,
  config,
  reloadKey,
  onSourceSaved,
  onLayoutChanged,
  preview,
  previewLoading,
}) {
  return (
    <div className="sg-zh-col2 sg-zh-col2--fieldsync">
      <div className="sg-zh-colmain">
        <ZohoLayoutEditor
          canManage={canManage}
          config={config}
          reloadKey={reloadKey}
          onSourceSaved={onSourceSaved}
          onLayoutChanged={onLayoutChanged}
        />
      </div>
      <div className="sg-zh-colside">
        <AgentPreview
          preview={preview}
          previewLoading={previewLoading}
          cascadeOn={config?.cascadeEnabled !== false}
        />
        <PublishedValues preview={preview} previewLoading={previewLoading} />
      </div>
    </div>
  );
}
