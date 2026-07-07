import { useCallback, useEffect, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ZohoLayoutEditor from "../components/zoho/ZohoLayoutEditor.jsx";
import {
  IconCheck,
  IconCopy,
  IconGlobe,
  IconInbox,
  IconLayers,
  IconRefresh,
  IconTrash,
  IconZap,
} from "../components/zoho/icons.jsx";
import { HealthRow, healthTone } from "../components/zoho/ZohoHealthRow.jsx";
import {
  deleteZohoInboundTicket,
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

function StatusPill({ status, okLabel = "OK", failLabel = "Failed", neverLabel = "Never" }) {
  if (!status) return <span className="status-pill muted">{neverLabel}</span>;
  const ok = status === "ok";
  return (
    <span className={`status-pill ${ok ? "ok" : "danger"}`}>{ok ? okLabel : failLabel}</span>
  );
}

export default function ZohoSyncPage({ canManage = false }) {
  const [config, setConfig] = useState(null);
  const [preview, setPreview] = useState(null);
  const [tickets, setTickets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [ticketsLoading, setTicketsLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [form, setForm] = useState(EMPTY_FORM);
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // Bumped by the Refresh button; the layout editor reloads (cache-bypassing)
  // whenever it changes.
  const [layoutReloadKey, setLayoutReloadKey] = useState(0);
  const [previewFilter, setPreviewFilter] = useState("");
  const [deletingTicketId, setDeletingTicketId] = useState(null);

  const webhookUrl =
    typeof window !== "undefined"
      ? `${window.location.origin.replace(/\/$/, "")}/api/zoho/inbound`
      : "/api/zoho/inbound";

  const load = useCallback(async (fresh = false) => {
    setLoading(true);
    setPreviewLoading(true);
    setTicketsLoading(true);
    setError("");
    // Fire everything at once, but only gate the page shell on the config (a
    // fast DB read). The preview (live cluster reads) and the ticket log stream
    // into their sections when they arrive — and rendering the shell early also
    // mounts the layout editor, so its Zoho read runs in parallel too.
    const previewPromise = getZohoPreview(fresh).catch(() => null);
    const ticketsPromise = listZohoInboundTickets(50).catch(() => ({ items: [] }));
    try {
      const cfg = await getZohoConfig();
      setConfig(cfg);
      setForm(formFromConfig(cfg));
    } catch (err) {
      setError(err.message || "Failed to load the Zoho integration.");
    } finally {
      setLoading(false);
    }
    previewPromise.then((prev) => {
      setPreview(prev);
      setPreviewLoading(false);
    });
    ticketsPromise.then((inbound) => {
      setTickets(inbound?.items || []);
      setTicketsLoading(false);
    });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Manual refresh: reload everything, bypassing the server-side read caches,
  // and tell the layout editor to re-read Zoho too.
  const runRefresh = async () => {
    setRefreshing(true);
    setNotice("");
    setLayoutReloadKey((k) => k + 1);
    try {
      await load(true);
    } finally {
      setRefreshing(false);
    }
  };

  const removeTicket = async (ticket) => {
    const label = ticket.ticketNumber || ticket.ticketId || `#${ticket.id}`;
    if (!window.confirm(`Delete inbound ticket ${label} from the log?`)) {
      return;
    }
    setError("");
    setNotice("");
    setDeletingTicketId(ticket.id);
    try {
      await deleteZohoInboundTicket(ticket.id);
      setTickets((prev) => prev.filter((t) => t.id !== ticket.id));
    } catch (err) {
      setError(err.message || "Failed to delete the inbound ticket.");
    } finally {
      setDeletingTicketId(null);
    }
  };

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

  const copyWebhookUrl = async (event) => {
    const input = event.currentTarget.closest(".sg-zh-url")?.querySelector("input");
    try {
      await navigator.clipboard.writeText(webhookUrl);
    } catch {
      input?.select();
      document.execCommand?.("copy");
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
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
  const namespaceCount = (preview?.namespaces || []).length;
  const resolvedCount = tickets.filter((t) => t.resolved).length;
  const unresolvedCount = tickets.length - resolvedCount;

  const previewItems = preview?.items || [];
  const filterQuery = previewFilter.trim().toLowerCase();
  const filteredItems = filterQuery
    ? previewItems.filter(
        (row) =>
          (row.label || "").toLowerCase().includes(filterQuery) ||
          (row.deploymentName || "").toLowerCase().includes(filterQuery) ||
          (row.namespace || "").toLowerCase().includes(filterQuery)
      )
    : previewItems;

  return (
    <div className="zoho-page">
      <PageTitle
        title="Zoho Integration"
        subtitle="Publish KubeSight's deployed workloads into the Zoho Desk DevOps Request dropdown, and resolve inbound tickets to the exact deployment."
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

      {/* -------------------------------------------------- KPI strip ------ */}
      <div className="sg-kpi-grid sg-zh-kpis">
        <div className="sg-kpi">
          <div className="sg-kpi-label">
            <IconZap /> Integration
          </div>
          <div className="sg-kpi-value">
            <span className={`status-pill ${enabled ? "ok" : "muted"}`}>
              {enabled ? "Enabled" : "Disabled"}
            </span>
          </div>
          <div className="sg-zh-kpi-sub">
            Auto-sync every {config?.syncIntervalMinutes || 30} min
          </div>
        </div>
        <div className="sg-kpi">
          <div className="sg-kpi-label">
            <IconLayers /> Applications
          </div>
          <div className="sg-kpi-value">
            <b>{previewLoading ? "…" : previewCount}</b>
          </div>
          <div className="sg-zh-kpi-sub">
            {config?.sourceClusterId ? (
              <>
                from <span className="sg-tag">{config.sourceClusterId}</span>
              </>
            ) : (
              "no source cluster selected yet"
            )}
          </div>
        </div>
        <div className="sg-kpi">
          <div className="sg-kpi-label">
            <IconGlobe /> Environments
          </div>
          <div className="sg-kpi-value">
            <b>{previewLoading ? "…" : namespaceCount}</b>
          </div>
          <div className="sg-zh-kpi-sub">namespaces published to Zoho</div>
        </div>
        <div className="sg-kpi">
          <div className="sg-kpi-label">
            <IconInbox /> Inbound tickets
          </div>
          <div className="sg-kpi-value">
            <b>{ticketsLoading ? "…" : tickets.length}</b>
            {!ticketsLoading && tickets.length > 0 ? (
              unresolvedCount > 0 ? (
                <span className="sg-delta sg-delta--down">{unresolvedCount} unresolved</span>
              ) : (
                <span className="sg-delta sg-delta--up">all resolved</span>
              )
            ) : null}
          </div>
          <div className="sg-zh-kpi-sub">
            {ticketsLoading ? "loading…" : `last ${tickets.length || 50} received`}
          </div>
        </div>
      </div>

      {/* -------------------------------------------------- Sync health ---- */}
      <section className="card">
        <div className="card-header-row">
          <h3>Sync health</h3>
          <div className="actions">
            <button
              type="button"
              className="secondary sg-zh-copy"
              onClick={runRefresh}
              disabled={refreshing}
              title="Re-read the config, live deployments, Zoho layout and ticket log"
            >
              <IconRefresh className={refreshing ? "sg-zh-spin" : undefined} />
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
            {canManage ? (
              <>
                <button type="button" className="secondary" onClick={openEdit}>
                  Edit configuration
                </button>
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
              </>
            ) : null}
          </div>
        </div>
        <div className="sg-zh-health">
          <HealthRow
            tone={healthTone(config?.lastTestStatus)}
            title="Connection to Zoho Desk"
            message={
              config?.lastTestMessage ||
              (config?.lastTestStatus ? "" : "Never tested — run a connection test.")
            }
            right={<StatusPill status={config?.lastTestStatus} />}
          />
          <HealthRow
            tone={healthTone(config?.lastSyncStatus)}
            title="Last sync"
            message={
              config?.lastSyncMessage ||
              (config?.lastSyncStatus ? "" : "No sync has run yet.")
            }
            right={<StatusPill status={config?.lastSyncStatus} />}
            time={config?.lastSyncAt ? new Date(config.lastSyncAt).toLocaleString() : ""}
          />
          <HealthRow
            tone={
              config?.cascadeEnabled === false ? "muted" : healthTone(config?.lastDependencyStatus)
            }
            title="Cascade (Environment → Application)"
            message={
              config?.cascadeEnabled === false
                ? "Cascade disabled in the configuration."
                : config?.lastDependencyMessage || ""
            }
            right={
              config?.cascadeEnabled === false ? (
                <span className="status-pill muted">Off</span>
              ) : (
                <StatusPill status={config?.lastDependencyStatus} neverLabel="Pending" />
              )
            }
          />
          <HealthRow
            tone={config?.sourceClusterId ? "ok" : "warn"}
            title="Dropdown source"
            message={
              config?.sourceClusterId
                ? ""
                : "No source cluster selected yet — use “Choose namespaces” on the Environment field below."
            }
            tags={
              config?.sourceClusterId ? (
                <>
                  <span className="sg-tag">{config.sourceClusterId}</span>
                  <span className="sg-zh-count">
                    {(config?.selectedNamespaces || []).length} namespace(s)
                  </span>
                </>
              ) : null
            }
          />
        </div>
      </section>

      {/* -------------------------------------------------- Inbound hint --- */}
      <section className="card">
        <div className="card-header-row">
          <h3>Inbound webhook</h3>
          {config?.inboundSecretConfigured ? (
            <span className="status-pill ok">Secret configured</span>
          ) : (
            <span className="status-pill warn">Open — no secret set</span>
          )}
        </div>
        <p className="muted">
          Configure a Zoho Desk workflow rule to POST new DevOps Request tickets to this URL. KubeSight
          reads the selected <code>Application</code> (deployment name) and <code>Environment</code>
          (namespace) values plus the tag field, and resolves the exact deployment. Send both fields —
          the app name alone can repeat across namespaces.
        </p>
        <div className="sg-zh-url">
          <input readOnly value={webhookUrl} onFocus={(e) => e.target.select()} className="mono" />
          <button type="button" className="secondary sg-zh-copy" onClick={copyWebhookUrl}>
            {copied ? <IconCheck /> : <IconCopy />}
            {copied ? "Copied" : "Copy URL"}
          </button>
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
        reloadKey={layoutReloadKey}
        onSourceSaved={async (updated) => {
          if (updated) setConfig((prev) => ({ ...(prev || {}), ...updated }));
          await refreshPreview();
        }}
      />

      {/* -------------------------------------------------- Preview -------- */}
      <section className="card">
        <div className="card-header-row">
          <h3>Application dropdown — live deployments</h3>
          <div className="sg-zh-head-tools">
            {!previewLoading && previewItems.length > 0 ? (
              <input
                type="search"
                className="sg-zh-filter"
                value={previewFilter}
                onChange={(e) => setPreviewFilter(e.target.value)}
                placeholder={`Filter ${previewItems.length} rows…`}
                aria-label="Filter deployments"
              />
            ) : null}
            <span className="sg-zh-count">
              {previewLoading
                ? "…"
                : filterQuery
                ? `${filteredItems.length} of ${previewItems.length}`
                : `${previewCount} unique`}
            </span>
          </div>
        </div>
        <p className="muted">
          The live deployments of your selected namespaces
          {preview?.sourceClusterId ? (
            <> on cluster <code>{preview.sourceClusterId}</code></>
          ) : null}
          , published into the Zoho <code>Application</code> field by <strong>name</strong> (e.g.{" "}
          <code>aims-ui</code>). Names shared across namespaces appear once; inside Zoho the list is
          filtered by the selected Environment (namespace) via the cascade, and a ticket resolves by
          Application name + Environment. The rows below show each name/namespace pairing.
        </p>
        {previewLoading ? (
          <p className="muted">Reading the live deployments from the source cluster…</p>
        ) : preview?.error ? (
          <p className="sg-zh-inline-error">{preview.error}</p>
        ) : previewCount === 0 ? (
          <EmptyState message="No deployments yet — pick a cluster and namespaces via “Choose namespaces” on the Environment field." />
        ) : (
          <div className="table-wrap sg-zh-tscroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Application dropdown value (as shown in Zoho)</th>
                  <th>Deployment</th>
                  <th>Namespace</th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{row.label}</td>
                    <td className="mono">{row.deploymentName}</td>
                    <td>
                      <span className="sg-tag">{row.namespace}</span>
                    </td>
                  </tr>
                ))}
                {filteredItems.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="muted">
                      No rows match “{previewFilter}”.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* -------------------------------------------------- Namespaces ---- */}
      <section className="card">
        <div className="card-header-row">
          <h3>Environment dropdown — namespaces</h3>
          <span className="sg-zh-count">{previewLoading ? "…" : namespaceCount}</span>
        </div>
        {previewLoading ? null : preview?.manageEnvironment ? (
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
        {previewLoading ? (
          <p className="muted">Loading…</p>
        ) : namespaceCount === 0 ? (
          <EmptyState message="No namespaces to publish yet." />
        ) : (
          <div className="sg-zh-tags">
            {(preview?.namespaces || []).map((ns) => (
              <span key={ns} className="sg-tag">{ns}</span>
            ))}
          </div>
        )}
      </section>

      {/* -------------------------------------------------- Inbound log --- */}
      <section className="card">
        <div className="card-header-row">
          <h3>Recent inbound tickets</h3>
          <span className="sg-zh-count">{ticketsLoading ? "…" : tickets.length}</span>
        </div>
        {ticketsLoading ? (
          <p className="muted">Loading…</p>
        ) : tickets.length === 0 ? (
          <EmptyState message="No tickets received yet." />
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Received</th>
                  <th>Ticket</th>
                  <th>Resolved deployment</th>
                  <th>Tag</th>
                  <th>Result</th>
                  {canManage ? <th aria-label="Actions" /> : null}
                </tr>
              </thead>
              <tbody>
                {tickets.map((t) => (
                  <tr key={t.id}>
                    <td className="sg-zh-htime">
                      {t.receivedAt ? new Date(t.receivedAt).toLocaleString() : ""}
                    </td>
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
                    <td>{t.tag ? <span className="sg-tag">{t.tag}</span> : "—"}</td>
                    <td>
                      {t.resolved ? (
                        <span className="status-pill ok">Resolved</span>
                      ) : (
                        <span className="status-pill danger" title={t.error || ""}>
                          Unresolved
                        </span>
                      )}
                    </td>
                    {canManage ? (
                      <td className="sg-zh-tactions">
                        <button
                          type="button"
                          className="btn-ghost sg-zh-tdel"
                          onClick={() => removeTicket(t)}
                          disabled={deletingTicketId === t.id}
                          title="Delete this entry from the inbound log"
                          aria-label={`Delete ticket ${t.ticketNumber || t.ticketId || t.id}`}
                        >
                          <IconTrash />
                        </button>
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* -------------------------------------------------- Edit modal ---- */}
      {showForm && canManage ? (
        <div className="modal-overlay" role="presentation" onClick={() => setShowForm(false)}>
          <section
            className="card modal-panel sg-zh-config"
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
