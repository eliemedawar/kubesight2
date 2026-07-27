import { useCallback, useEffect, useState } from "react";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { IconRefresh } from "../components/zoho/icons.jsx";
import { minutesToNextSync, timeAgo } from "../components/zoho/common.jsx";
import ZohoFlowStrip from "../components/zoho/ZohoFlowStrip.jsx";
import ZohoOverviewTab from "../components/zoho/ZohoOverviewTab.jsx";
import ZohoFieldSyncTab from "../components/zoho/ZohoFieldSyncTab.jsx";
import ZohoTicketsTab from "../components/zoho/ZohoTicketsTab.jsx";
import ZohoSettingsTab from "../components/zoho/ZohoSettingsTab.jsx";
import { ACTIVE_RUN_STATUSES } from "../components/zoho/ZohoRunDetail.jsx";
import {
  cancelAutomationRun,
  deleteZohoInboundTicket,
  getJenkinsConfig,
  getZohoConfig,
  getZohoPreview,
  listAutomationRuns,
  listZohoInboundTickets,
  startAutomationRun,
  syncZohoNow,
  testJenkinsConnection,
  testZohoConnection,
  updateJenkinsConfig,
  updateZohoConfig,
} from "../api/zohoApi.js";

// Config keys the settings form owns. Secrets are write-only: blank = keep current.
const EMPTY_FORM = {
  enabled: false,
  orgId: "",
  layoutId: "",
  appFieldId: "",
  appFieldApiName: "cf_application",
  environmentFieldId: "",
  environmentFieldApiName: "cf_environment",
  tagFieldApiName: "cf_tag",
  variableFieldId: "",
  variableFieldApiName: "cf_variable",
  valueFieldApiName: "cf_value",
  apiBase: "https://desk.zoho.com/api/v1",
  accountsBase: "https://accounts.zoho.com",
  tokenEndpoint: "https://accounts.zoho.com/oauth/v2/token",
  departmentId: "",
  statusFilter: "active,degraded",
  syncIntervalMinutes: 30,
  syncApplication: true,
  syncEnvironment: true,
  syncVariables: false,
  cascadeEnabled: true,
  clientId: "",
  clientSecret: "",
  refreshToken: "",
  inboundSecret: "",
  ticketWritebackEnabled: false,
  ticketStatusStarted: "Open",
  ticketStatusDeployed: "Closed",
  ticketStatusFailed: "Failed",
  ticketStatusCancelled: "Canceled",
  ticketOwnerEmail: "zagent@areeba.com",
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
    variableFieldId: cfg.variableFieldId || "",
    variableFieldApiName: cfg.variableFieldApiName || "cf_variable",
    valueFieldApiName: cfg.valueFieldApiName || "cf_value",
    apiBase: cfg.apiBase || "https://desk.zoho.com/api/v1",
    accountsBase: cfg.accountsBase || "https://accounts.zoho.com",
    tokenEndpoint: cfg.tokenEndpoint || "https://accounts.zoho.com/oauth/v2/token",
    departmentId: cfg.departmentId || "",
    statusFilter: (cfg.statusFilter || []).join(", "),
    syncIntervalMinutes: cfg.syncIntervalMinutes || 30,
    syncApplication: cfg.syncApplication !== false,
    syncEnvironment: cfg.syncEnvironment !== false,
    syncVariables: Boolean(cfg.syncVariables),
    cascadeEnabled: cfg.cascadeEnabled !== false,
    clientId: cfg.clientId || "",
    clientSecret: "",
    refreshToken: "",
    inboundSecret: "",
    ticketWritebackEnabled: Boolean(cfg.ticketWritebackEnabled),
    ticketStatusStarted: cfg.ticketStatusStarted || "Open",
    ticketStatusDeployed: cfg.ticketStatusDeployed || "Closed",
    ticketStatusFailed: cfg.ticketStatusFailed || "Failed",
    ticketStatusCancelled: cfg.ticketStatusCancelled || "Canceled",
    ticketOwnerEmail: cfg.ticketOwnerEmail || "",
  };
}

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "fieldsync", label: "Field sync" },
  { key: "tickets", label: "Tickets & runs" },
  { key: "settings", label: "Settings" },
];

export default function ZohoSyncPage({ canManage = false }) {
  const [config, setConfig] = useState(null);
  const [preview, setPreview] = useState(null);
  const [tickets, setTickets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [ticketsLoading, setTicketsLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState("overview");

  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // Bumped by the Refresh button; the layout editor reloads (cache-bypassing)
  // whenever it changes.
  const [layoutReloadKey, setLayoutReloadKey] = useState(0);
  const [deletingTicketId, setDeletingTicketId] = useState(null);

  // Deploy automation (Jenkins router + per-ticket runs).
  const [jenkins, setJenkins] = useState(null);
  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [savingJenkins, setSavingJenkins] = useState(false);
  const [testingJenkins, setTestingJenkins] = useState(false);
  const [startingTicketId, setStartingTicketId] = useState(null);
  const [cancellingRunId, setCancellingRunId] = useState(null);

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
    // into their sections when they arrive.
    const previewPromise = getZohoPreview(fresh).catch(() => null);
    // The backend prunes the inbound log to the 10 newest tickets on every
    // webhook delivery — ask for exactly that window.
    const ticketsPromise = listZohoInboundTickets(10).catch(() => ({ items: [] }));
    const jenkinsPromise = getJenkinsConfig().catch(() => null);
    const runsPromise = listAutomationRuns(50).catch(() => ({ items: [] }));
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
    jenkinsPromise.then((jk) => setJenkins(jk));
    runsPromise.then((res) => {
      setRuns(res?.items || []);
      setRunsLoading(false);
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

  // Poll active automation runs every 10s so the pipeline chips move without a
  // manual refresh; stops on its own once every run is terminal.
  const hasActiveRun = runs.some((r) => ACTIVE_RUN_STATUSES.has(r.status));
  useEffect(() => {
    if (!hasActiveRun) return undefined;
    const timer = setInterval(async () => {
      try {
        const res = await listAutomationRuns(50);
        setRuns(res?.items || []);
      } catch {
        /* transient — next poll retries */
      }
    }, 10000);
    return () => clearInterval(timer);
  }, [hasActiveRun]);

  const refreshRuns = async () => {
    try {
      const res = await listAutomationRuns(50);
      setRuns(res?.items || []);
    } catch {
      /* best-effort */
    }
  };

  const saveJenkins = async (payload) => {
    setSavingJenkins(true);
    setError("");
    try {
      const updated = await updateJenkinsConfig(payload);
      setJenkins(updated);
      setNotice("Jenkins connection saved.");
      return true;
    } catch (err) {
      setError(err.message || "Failed to save the Jenkins connection.");
      return false;
    } finally {
      setSavingJenkins(false);
    }
  };

  const runJenkinsTest = async () => {
    setTestingJenkins(true);
    setError("");
    setNotice("");
    try {
      const result = await testJenkinsConnection();
      setJenkins((prev) => ({ ...(prev || {}), ...result }));
      if (result.status === "ok") {
        setNotice(result.message || "Jenkins connection OK.");
      } else {
        setError(result.message || "Jenkins connection test failed.");
      }
    } catch (err) {
      setError(err.message || "Jenkins connection test failed.");
    } finally {
      setTestingJenkins(false);
    }
  };

  const runTicketAutomation = async (ticket) => {
    setError("");
    setNotice("");
    setStartingTicketId(ticket.id);
    try {
      await startAutomationRun(ticket.id);
      setNotice(`Automation started for ${ticket.ticketNumber || ticket.ticketId || "the ticket"}.`);
      await refreshRuns();
    } catch (err) {
      setError(err.message || "Failed to start the automation run.");
    } finally {
      setStartingTicketId(null);
    }
  };

  const cancelRun = async (run) => {
    if (!window.confirm(`Cancel automation run for ${run.ticketNumber || `#${run.id}`}?`)) {
      return;
    }
    setError("");
    setCancellingRunId(run.id);
    try {
      await cancelAutomationRun(run.id);
      await refreshRuns();
    } catch (err) {
      setError(err.message || "Failed to cancel the run.");
    } finally {
      setCancellingRunId(null);
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

  const saveConfig = async () => {
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
        variableFieldId: form.variableFieldId,
        variableFieldApiName: form.variableFieldApiName,
        valueFieldApiName: form.valueFieldApiName,
        apiBase: form.apiBase,
        accountsBase: form.accountsBase,
        tokenEndpoint: form.tokenEndpoint,
        departmentId: form.departmentId,
        statusFilter: form.statusFilter,
        syncIntervalMinutes: Number(form.syncIntervalMinutes) || 30,
        syncApplication: form.syncApplication,
        syncEnvironment: form.syncEnvironment,
        syncVariables: form.syncVariables,
        cascadeEnabled: form.cascadeEnabled,
        clientId: form.clientId,
        ticketWritebackEnabled: form.ticketWritebackEnabled,
        ticketStatusStarted: form.ticketStatusStarted,
        ticketStatusDeployed: form.ticketStatusDeployed,
        ticketStatusFailed: form.ticketStatusFailed,
        ticketStatusCancelled: form.ticketStatusCancelled,
        ticketOwnerEmail: form.ticketOwnerEmail,
      };
      // Secrets: only send when the operator typed one (blank keeps current).
      if (form.clientSecret.trim()) payload.clientSecret = form.clientSecret.trim();
      if (form.refreshToken.trim()) payload.refreshToken = form.refreshToken.trim();
      if (form.inboundSecret.trim()) payload.inboundSecret = form.inboundSecret.trim();

      const updated = await updateZohoConfig(payload);
      setConfig(updated);
      setForm(formFromConfig(updated));
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

  if (loading) {
    return (
      <div className="zoho-page">
        <header className="sg-zh-cmdbar">
          <div>
            <h2 className="sg-zh-cmdtitle">Zoho Integration</h2>
          </div>
        </header>
        <p className="muted">Loading…</p>
      </div>
    );
  }

  const enabled = Boolean(config?.enabled);
  const dirty = JSON.stringify(form) !== JSON.stringify(formFromConfig(config || {}));
  const nextSync = minutesToNextSync(config);

  // Attention badge on the Tickets & runs tab: unresolved tickets + runs
  // waiting on a human.
  const unresolvedCount = tickets.filter((t) => !t.resolved).length;
  const awaitingCount = runs.filter((r) => r.status === "awaiting_approval").length;
  const attentionCount = unresolvedCount + awaitingCount;

  return (
    <div className="zoho-page">
      {/* ------------------------------------------------ Command bar ------ */}
      <header className="sg-zh-cmdbar">
        <div>
          <h2 className="sg-zh-cmdtitle">
            Zoho Integration
            <span className={`status-pill ${enabled ? "ok" : "muted"}`}>
              {enabled ? "On" : "Off"}
            </span>
          </h2>
          <p className="sg-zh-cmdsub">
            {config?.orgId ? (
              <>
                Desk org <span className="mono">{config.orgId}</span>
              </>
            ) : (
              "Not connected yet"
            )}
            <span className="sg-zh-cmdsep">·</span>
            {config?.lastSyncAt ? `Synced ${timeAgo(config.lastSyncAt)}` : "Never synced"}
            {nextSync != null ? (
              <>
                <span className="sg-zh-cmdsep">·</span>
                next auto-sync in {nextSync} min
              </>
            ) : null}
          </p>
        </div>
        <div className="sg-zh-cmdactions">
          <button
            type="button"
            className="btn-ghost sg-zh-copy"
            onClick={runRefresh}
            disabled={refreshing}
            title="Re-read the config, live deployments, Zoho layout and ticket log"
          >
            <IconRefresh className={refreshing ? "sg-zh-spin" : undefined} />
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
          {canManage ? (
            <>
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
      </header>

      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      {notice ? (
        <div className="banner banner-success" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      {/* ------------------------------------------------ Flow strip ------- */}
      <ZohoFlowStrip
        config={config}
        preview={preview}
        previewLoading={previewLoading}
        tickets={tickets}
        ticketsLoading={ticketsLoading}
        runs={runs}
        jenkins={jenkins}
        onNavigate={setTab}
      />

      {/* ------------------------------------------------ Sub-tabs --------- */}
      <nav className="sg-zh-subtabs" role="tablist" aria-label="Zoho integration sections">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`sg-zh-subtab ${tab === t.key ? "sg-zh-subtab--on" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === "tickets" && attentionCount > 0 ? (
              <span className="sg-zh-tabcnt">{attentionCount}</span>
            ) : null}
          </button>
        ))}
      </nav>

      {/* ------------------------------------------------ Rooms ------------ */}
      {tab === "overview" ? (
        <ZohoOverviewTab config={config} tickets={tickets} runs={runs} />
      ) : null}

      {tab === "fieldsync" ? (
        <ZohoFieldSyncTab
          canManage={canManage}
          config={config}
          reloadKey={layoutReloadKey}
          onSourceSaved={async (updated) => {
            if (updated) setConfig((prev) => ({ ...(prev || {}), ...updated }));
            await refreshPreview();
          }}
          // A layout edit can change which fields the sync publishes into, so
          // the preview beside the editor has to be re-read too.
          onLayoutChanged={refreshPreview}
          preview={preview}
          previewLoading={previewLoading}
        />
      ) : null}

      {tab === "tickets" ? (
        <ZohoTicketsTab
          canManage={canManage}
          tickets={tickets}
          ticketsLoading={ticketsLoading}
          runs={runs}
          runsLoading={runsLoading}
          webhookUrl={webhookUrl}
          inboundSecretConfigured={Boolean(config?.inboundSecretConfigured)}
          onRunTicket={runTicketAutomation}
          startingTicketId={startingTicketId}
          onCancelRun={cancelRun}
          cancellingRunId={cancellingRunId}
          onDeleteTicket={removeTicket}
          deletingTicketId={deletingTicketId}
        />
      ) : null}

      {tab === "settings" ? (
        <ZohoSettingsTab
          canManage={canManage}
          config={config}
          form={form}
          setField={setField}
          onSaveConfig={saveConfig}
          savingConfig={saving}
          dirty={dirty}
          onDiscard={() => setForm(formFromConfig(config || {}))}
          webhookUrl={webhookUrl}
          jenkins={jenkins}
          onSaveJenkins={saveJenkins}
          savingJenkins={savingJenkins}
          onTestJenkins={runJenkinsTest}
          testingJenkins={testingJenkins}
        />
      ) : null}
    </div>
  );
}
