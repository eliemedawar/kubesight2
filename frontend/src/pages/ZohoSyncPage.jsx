import { useCallback, useEffect, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ZohoLayoutEditor from "../components/zoho/ZohoLayoutEditor.jsx";
import {
  getZohoConfig,
  getZohoPreview,
  listZohoInboundTickets,
  syncZohoNow,
  testZohoConnection,
  updateZohoConfig,
} from "../api/zohoApi.js";

// Config keys the edit form owns. Secrets are write-only: blank = keep current.
const EMPTY_FORM = {
  enabled: false,
  orgId: "",
  layoutId: "",
  appFieldId: "",
  appFieldApiName: "cf_application",
  environmentFieldId: "",
  environmentFieldApiName: "cf_environment",
  tagFieldApiName: "cf_tag",
  apiBase: "https://desk.zoho.com/api/v1",
  accountsBase: "https://accounts.zoho.com",
  tokenEndpoint: "https://accounts.zoho.com/oauth/v2/token",
  departmentId: "",
  statusFilter: "active,degraded",
  syncIntervalMinutes: 30,
  syncApplication: true,
  syncEnvironment: true,
  cascadeEnabled: true,
  clientId: "",
  clientSecret: "",
  refreshToken: "",
  inboundSecret: "",
};

function formFromConfig(cfg) {
  return {
    enabled: Boolean(cfg.enabled),
    orgId: cfg.orgId || "",
    layoutId: cfg.layoutId || "",
    appFieldId: cfg.appFieldId || "",
    appFieldApiName: cfg.appFieldApiName || "cf_application",
    environmentFieldId: cfg.environmentFieldId || "",
    environmentFieldApiName: cfg.environmentFieldApiName || "cf_environment",
    tagFieldApiName: cfg.tagFieldApiName || "cf_tag",
    apiBase: cfg.apiBase || "https://desk.zoho.com/api/v1",
    accountsBase: cfg.accountsBase || "https://accounts.zoho.com",
    tokenEndpoint: cfg.tokenEndpoint || "https://accounts.zoho.com/oauth/v2/token",
    departmentId: cfg.departmentId || "",
    statusFilter: (cfg.statusFilter || []).join(", "),
    syncIntervalMinutes: cfg.syncIntervalMinutes || 30,
    syncApplication: cfg.syncApplication !== false,
    syncEnvironment: cfg.syncEnvironment !== false,
    cascadeEnabled: cfg.cascadeEnabled !== false,
    clientId: cfg.clientId || "",
    clientSecret: "",
    refreshToken: "",
    inboundSecret: "",
  };
}

function StatusBadge({ status }) {
  if (!status) return <span className="muted">Never</span>;
  const ok = status === "ok";
  return (
    <span className={`badge ${ok ? "status-ok" : "status-error"}`}>{ok ? "OK" : "Failed"}</span>
  );
}

export default function ZohoSyncPage({ canManage = false }) {
  const [config, setConfig] = useState(null);
  const [preview, setPreview] = useState(null);
  const [tickets, setTickets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [form, setForm] = useState(EMPTY_FORM);
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const webhookUrl =
    typeof window !== "undefined"
      ? `${window.location.origin.replace(/\/$/, "")}/api/zoho/inbound`
      : "/api/zoho/inbound";

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [cfg, prev, inbound] = await Promise.all([
        getZohoConfig(),
        getZohoPreview().catch(() => null),
        listZohoInboundTickets(50).catch(() => ({ items: [] })),
      ]);
      setConfig(cfg);
      setForm(formFromConfig(cfg));
      setPreview(prev);
      setTickets(inbound?.items || []);
    } catch (err) {
      setError(err.message || "Failed to load the Zoho integration.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const setField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const refreshPreview = async () => {
    try {
      setPreview(await getZohoPreview());
    } catch {
      /* preview is best-effort */
    }
  };

  const submit = async (event) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const payload = {
        enabled: form.enabled,
        orgId: form.orgId,
        layoutId: form.layoutId,
        appFieldId: form.appFieldId,
        appFieldApiName: form.appFieldApiName,
        environmentFieldId: form.environmentFieldId,
        environmentFieldApiName: form.environmentFieldApiName,
        tagFieldApiName: form.tagFieldApiName,
        apiBase: form.apiBase,
        accountsBase: form.accountsBase,
        tokenEndpoint: form.tokenEndpoint,
        departmentId: form.departmentId,
        statusFilter: form.statusFilter,
        syncIntervalMinutes: Number(form.syncIntervalMinutes) || 30,
        syncApplication: form.syncApplication,
        syncEnvironment: form.syncEnvironment,
        cascadeEnabled: form.cascadeEnabled,
        clientId: form.clientId,
      };
      // Secrets: only send when the operator typed one (blank keeps current).
      if (form.clientSecret.trim()) payload.clientSecret = form.clientSecret.trim();
      if (form.refreshToken.trim()) payload.refreshToken = form.refreshToken.trim();
      if (form.inboundSecret.trim()) payload.inboundSecret = form.inboundSecret.trim();

      const updated = await updateZohoConfig(payload);
      setConfig(updated);
      setForm(formFromConfig(updated));
      setShowForm(false);
      setNotice("Configuration saved.");
      refreshPreview();
    } catch (err) {
      setError(err.message || "Failed to save the configuration.");
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setError("");
    setNotice("");
    try {
      const result = await testZohoConnection();
      if (result.status === "ok") {
        setNotice(result.message || "Connection successful.");
      } else {
        setError(result.message || "Connection test failed.");
      }
      setConfig((prev) => ({ ...(prev || {}), ...result }));
    } catch (err) {
      setError(err.message || "Connection test failed.");
    } finally {
      setTesting(false);
    }
  };

  const runSync = async () => {
    setSyncing(true);
    setError("");
    setNotice("");
    try {
      const result = await syncZohoNow();
      if (result.status === "ok") {
        setNotice(result.message || "Sync complete.");
      } else {
        setError(result.message || "Sync failed.");
      }
      setConfig((prev) => ({ ...(prev || {}), ...result }));
      refreshPreview();
    } catch (err) {
      setError(err.message || "Sync failed.");
    } finally {
      setSyncing(false);
    }
  };

  const openEdit = () => {
    setForm(formFromConfig(config || {}));
    setError("");
    setNotice("");
    setShowForm(true);
  };

  if (loading) {
    return (
      <div className="zoho-page">
        <PageTitle title="Zoho Integration" subtitle="DevOps Request field sync" />
        <p className="muted">Loading…</p>
      </div>
    );
  }

  const enabled = Boolean(config?.enabled);
  const previewCount = preview?.count ?? 0;

  return (
    <div className="zoho-page">
      <PageTitle
        title="Zoho Integration"
        subtitle="Publish KubeSight's deployed workloads into the Zoho Desk DevOps Request dropdown, and resolve inbound tickets to the exact deployment."
        actionLabel={canManage ? "Edit configuration" : undefined}
        onAction={canManage ? openEdit : undefined}
      />

      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      {notice ? (
        <div className="banner banner-success" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      {/* -------------------------------------------------- Status --------- */}
      <section className="card">
        <div className="card-header-row">
          <h3>Sync status</h3>
          {canManage ? (
            <div className="actions">
              <button type="button" className="secondary" onClick={runTest} disabled={testing}>
                {testing ? "Testing…" : "Test connection"}
              </button>
              <button
                type="button"
                className="primary"
                onClick={runSync}
                disabled={syncing || !enabled}
                title={enabled ? "" : "Enable the integration first"}
              >
                {syncing ? "Syncing…" : "Sync now"}
              </button>
            </div>
          ) : null}
        </div>
        <dl className="detail-grid">
          <div>
            <dt>Integration</dt>
            <dd>
              <span className={`badge ${enabled ? "status-ok" : "status-muted"}`}>
                {enabled ? "Enabled" : "Disabled"}
              </span>
            </dd>
          </div>
          <div>
            <dt>Last sync</dt>
            <dd>
              <StatusBadge status={config?.lastSyncStatus} />{" "}
              {config?.lastSyncAt ? new Date(config.lastSyncAt).toLocaleString() : ""}
              {config?.lastSyncMessage ? (
                <div className="muted">{config.lastSyncMessage}</div>
              ) : null}
            </dd>
          </div>
          <div>
            <dt>Last connection test</dt>
            <dd>
              <StatusBadge status={config?.lastTestStatus} />
              {config?.lastTestMessage ? (
                <div className="muted">{config.lastTestMessage}</div>
              ) : null}
            </dd>
          </div>
          <div>
            <dt>Auto-sync</dt>
            <dd>
              Every {config?.syncIntervalMinutes || 30} min ·{" "}
              {config?.sourceClusterId ? (
                <>
                  cluster <code>{config.sourceClusterId}</code> ·{" "}
                  {(config?.selectedNamespaces || []).length} namespace(s)
                </>
              ) : (
                "no source cluster selected yet"
              )}
            </dd>
          </div>
          <div>
            <dt>Cascade (Env → App)</dt>
            <dd>
              <span
                className={`badge ${
                  config?.lastDependencyStatus === "ok"
                    ? "status-ok"
                    : config?.lastDependencyStatus === "error"
                    ? "status-error"
                    : "status-muted"
                }`}
              >
                {config?.cascadeEnabled === false
                  ? "Off"
                  : config?.lastDependencyStatus || "Pending"}
              </span>
              {config?.lastDependencyMessage ? (
                <div className="muted">{config.lastDependencyMessage}</div>
              ) : null}
            </dd>
          </div>
        </dl>
      </section>

      {/* -------------------------------------------------- Inbound hint --- */}
      <section className="card">
        <h3>Inbound webhook</h3>
        <p className="muted">
          Configure a Zoho Desk workflow rule to POST new DevOps Request tickets to this URL. KubeSight
          reads the selected Application value's trailing deployment id (or a <code>deployment_id</code> field)
          plus the tag field, and resolves the exact deployment.
        </p>
        <div className="inline-form">
          <input readOnly value={webhookUrl} onFocus={(e) => e.target.select()} className="mono" />
        </div>
        <p className="field-hint">
          Send the shared secret in the <code>X-Zoho-Secret</code> header.{" "}
          {config?.inboundSecretConfigured
            ? "A secret is configured."
            : "No secret set yet — the webhook is currently open; set one in the configuration."}
        </p>
      </section>

      {/* -------------------------------------------------- Layout editor - */}
      <ZohoLayoutEditor
        canManage={canManage}
        config={config}
        onSourceSaved={async (updated) => {
          if (updated) setConfig((prev) => ({ ...(prev || {}), ...updated }));
          await refreshPreview();
        }}
      />

      {/* -------------------------------------------------- Preview -------- */}
      <section className="card">
        <h3>Application dropdown — live deployments ({previewCount})</h3>
        <p className="muted">
          The live deployments of your selected namespaces
          {preview?.sourceClusterId ? (
            <> on cluster <code>{preview.sourceClusterId}</code></>
          ) : null}
          . Each is published into the Zoho <code>Application</code> field on the next sync and carries
          a stable id; a ticket resolves to that exact deployment. Inside Zoho the list is filtered by
          the selected Environment (namespace) via the cascade.
        </p>
        {preview?.error ? (
          <div className="field-hint status-error">{preview.error}</div>
        ) : previewCount === 0 ? (
          <EmptyState message="No deployments yet — pick a cluster and namespaces via “Choose namespaces” on the Environment field." />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Application dropdown value (as shown in Zoho)</th>
                <th>Deployment</th>
                <th>Namespace</th>
              </tr>
            </thead>
            <tbody>
              {(preview?.items || []).map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.label}</td>
                  <td className="mono">{row.deploymentName}</td>
                  <td>{row.namespace}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* -------------------------------------------------- Namespaces ---- */}
      <section className="card">
        <h3>Environment dropdown — namespaces ({(preview?.namespaces || []).length})</h3>
        {preview?.manageEnvironment ? (
          <p className="muted">
            The namespaces you selected — published into the Zoho <code>Environment</code> field. Change
            them via “Choose namespaces” on the Environment field in the layout editor above.
          </p>
        ) : (
          <p className="field-hint">
            No <code>Environment</code> field ID configured — namespaces won't be published. Set it in the
            configuration to publish the namespace list too.
          </p>
        )}
        {(preview?.namespaces || []).length === 0 ? (
          <EmptyState message="No namespaces to publish yet." />
        ) : (
          <div className="chip-row">
            {(preview?.namespaces || []).map((ns) => (
              <span key={ns} className="badge status-muted mono">{ns}</span>
            ))}
          </div>
        )}
      </section>

      {/* -------------------------------------------------- Inbound log --- */}
      <section className="card">
        <h3>Recent inbound tickets</h3>
        {tickets.length === 0 ? (
          <EmptyState message="No tickets received yet." />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Received</th>
                <th>Ticket</th>
                <th>Resolved deployment</th>
                <th>Tag</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {tickets.map((t) => (
                <tr key={t.id}>
                  <td>{t.receivedAt ? new Date(t.receivedAt).toLocaleString() : ""}</td>
                  <td>{t.ticketNumber || t.ticketId || "—"}</td>
                  <td>
                    {t.resolved ? (
                      <span className="mono" title={t.targetName || ""}>
                        {t.deploymentName || t.targetName || `#${t.targetId}`}
                        {t.namespace ? ` (${t.namespace})` : ""}
                      </span>
                    ) : (
                      <span className="muted">{t.rawAppValue || "—"}</span>
                    )}
                  </td>
                  <td>{t.tag || "—"}</td>
                  <td>
                    {t.resolved ? (
                      <span className="badge status-ok">Resolved</span>
                    ) : (
                      <span className="badge status-error" title={t.error || ""}>
                        Unresolved
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* -------------------------------------------------- Edit modal ---- */}
      {showForm && canManage ? (
        <div className="modal-overlay" role="presentation" onClick={() => setShowForm(false)}>
          <section
            className="card modal-panel"
            role="dialog"
            aria-labelledby="zoho-form-title"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="modal-header">
              <div>
                <h3 id="zoho-form-title">Zoho integration configuration</h3>
                <p className="muted">
                  IDs come from DEVOPS-REQUEST-FIELD-SYNC-CONFIG.md. Secrets are stored encrypted; leave a
                  secret field blank to keep the current value.
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setShowForm(false)}
                aria-label="Close"
              >
                ✕
              </button>
            </header>

            <form className="settings-form" onSubmit={submit}>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setField("enabled", e.target.checked)}
                />
                Enabled (allows scheduled + manual sync)
              </label>

              <h4>Zoho connection</h4>
              <label>
                Org ID
                <input value={form.orgId} onChange={(e) => setField("orgId", e.target.value)} placeholder="854214247" />
              </label>
              <label>
                Layout ID (DevOps Request)
                <input value={form.layoutId} onChange={(e) => setField("layoutId", e.target.value)} placeholder="999149000010342586" />
              </label>
              <label>
                Application field ID (deployments dropdown)
                <input value={form.appFieldId} onChange={(e) => setField("appFieldId", e.target.value)} placeholder="999149000010343250" />
              </label>
              <label>
                Application field API name
                <input value={form.appFieldApiName} onChange={(e) => setField("appFieldApiName", e.target.value)} placeholder="cf_application" />
              </label>
              <label>
                Environment field ID (namespaces dropdown — optional)
                <input value={form.environmentFieldId} onChange={(e) => setField("environmentFieldId", e.target.value)} placeholder="999149000010343580" />
                <span className="field-hint">Leave blank to not publish the namespace list.</span>
              </label>
              <label>
                Environment field API name
                <input value={form.environmentFieldApiName} onChange={(e) => setField("environmentFieldApiName", e.target.value)} placeholder="cf_environment" />
              </label>
              <label>
                Tag field API name (inbound version)
                <input value={form.tagFieldApiName} onChange={(e) => setField("tagFieldApiName", e.target.value)} placeholder="cf_tag" />
              </label>
              <label>
                Department ID (optional)
                <input value={form.departmentId} onChange={(e) => setField("departmentId", e.target.value)} />
              </label>
              <label>
                Desk API base
                <input value={form.apiBase} onChange={(e) => setField("apiBase", e.target.value)} />
              </label>
              <label>
                Token endpoint
                <input value={form.tokenEndpoint} onChange={(e) => setField("tokenEndpoint", e.target.value)} />
              </label>

              <h4>OAuth (server-to-server Self Client, acting as zagent)</h4>
              <label>
                Client ID
                <input value={form.clientId} onChange={(e) => setField("clientId", e.target.value)} autoComplete="off" />
              </label>
              <label>
                Client Secret
                <input
                  type="password"
                  value={form.clientSecret}
                  onChange={(e) => setField("clientSecret", e.target.value)}
                  placeholder={config?.clientSecretConfigured ? "•••• (leave blank to keep)" : ""}
                  autoComplete="new-password"
                />
              </label>
              <label>
                Refresh Token
                <input
                  type="password"
                  value={form.refreshToken}
                  onChange={(e) => setField("refreshToken", e.target.value)}
                  placeholder={config?.refreshTokenConfigured ? "•••• (leave blank to keep)" : ""}
                  autoComplete="new-password"
                />
              </label>

              <h4>Sync behaviour</h4>
              <p className="field-hint">
                Choose which fields KubeSight publishes. A field left off here is yours to edit
                manually in the layout editor — the sync won't touch it.
              </p>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.syncApplication}
                  onChange={(e) => setField("syncApplication", e.target.checked)}
                />
                Publish deployments → Application field
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.syncEnvironment}
                  onChange={(e) => setField("syncEnvironment", e.target.checked)}
                />
                Publish namespaces → Environment field
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.cascadeEnabled}
                  onChange={(e) => setField("cascadeEnabled", e.target.checked)}
                />
                Cascade: filter Application by the selected Environment (needs both fields published;
                requires <code>Desk.settings.CREATE</code> on the Zoho token)
              </label>
              <label>
                Auto-sync interval (minutes)
                <input
                  type="number"
                  min="1"
                  value={form.syncIntervalMinutes}
                  onChange={(e) => setField("syncIntervalMinutes", e.target.value)}
                />
              </label>

              <h4>Inbound webhook</h4>
              <label>
                Shared secret (X-Zoho-Secret header)
                <input
                  type="password"
                  value={form.inboundSecret}
                  onChange={(e) => setField("inboundSecret", e.target.value)}
                  placeholder={config?.inboundSecretConfigured ? "•••• (leave blank to keep)" : ""}
                  autoComplete="new-password"
                />
              </label>

              <div className="modal-actions">
                <button type="button" className="secondary" onClick={() => setShowForm(false)}>
                  Cancel
                </button>
                <button type="submit" className="primary" disabled={saving}>
                  {saving ? "Saving…" : "Save configuration"}
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}
