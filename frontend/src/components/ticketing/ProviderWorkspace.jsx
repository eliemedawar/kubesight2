import { useCallback, useEffect, useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { minutesToNextSync, timeAgo } from "../zoho/common.jsx";
import { IconArrowLeft, IconRefresh } from "../zoho/icons.jsx";
import ZohoFieldSyncTab from "../zoho/ZohoFieldSyncTab.jsx";
import ZohoFlowStrip from "../zoho/ZohoFlowStrip.jsx";
import ZohoOverviewTab from "../zoho/ZohoOverviewTab.jsx";
import { ACTIVE_RUN_STATUSES } from "../zoho/ZohoRunDetail.jsx";
import ZohoTicketsTab from "../zoho/ZohoTicketsTab.jsx";
import { useTicketing } from "./TicketingContext.jsx";

// One provider's workspace: command bar → flow strip → three rooms. This is the
// page the Ticketing tab opens when a provider card is picked, and it is
// provider-agnostic — everything vendor-specific reaches it through
// `useTicketing()` (the bound API client and the capability flags).

// Connecting the provider — credentials, field ids, the webhook, Jenkins — moved
// to Settings → Integrations, where every outside system is configured. What is
// left here is the work you do once it is connected.
const TABS = [
  { key: "overview", label: "Overview" },
  { key: "fieldsync", label: "Field sync" },
  { key: "tickets", label: "Tickets & runs" },
];

export default function ProviderWorkspace({ canManage = false, onBack }) {
  const { key: providerKey, name: providerName, formNoun, api } = useTicketing();

  const [config, setConfig] = useState(null);
  const [preview, setPreview] = useState(null);
  const [tickets, setTickets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [ticketsLoading, setTicketsLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState("overview");

  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // Bumped by the Refresh button; the form editor reloads (cache-bypassing)
  // whenever it changes.
  const [layoutReloadKey, setLayoutReloadKey] = useState(0);
  const [deletingTicketId, setDeletingTicketId] = useState(null);

  // Deploy automation (Jenkins router + per-ticket runs).
  const [jenkins, setJenkins] = useState(null);
  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [startingTicketId, setStartingTicketId] = useState(null);
  const [cancellingRunId, setCancellingRunId] = useState(null);

  const webhookUrl =
    typeof window !== "undefined"
      ? `${window.location.origin.replace(/\/$/, "")}/api/ticketing/${providerKey}/inbound`
      : `/api/ticketing/${providerKey}/inbound`;

  const load = useCallback(
    async (fresh = false) => {
      setLoading(true);
      setPreviewLoading(true);
      setTicketsLoading(true);
      setError("");
      // Fire everything at once, but only gate the page shell on the config (a
      // fast DB read). The preview (live cluster reads) and the ticket log stream
      // into their sections when they arrive.
      const previewPromise = api.getPreview(fresh).catch(() => null);
      // The backend prunes the inbound log to the 10 newest tickets on every
      // webhook delivery — ask for exactly that window.
      const ticketsPromise = api.listInboundTickets(10).catch(() => ({ items: [] }));
      const jenkinsPromise = api.getJenkinsConfig().catch(() => null);
      const runsPromise = api.listAutomationRuns(50).catch(() => ({ items: [] }));
      try {
        setConfig(await api.getConfig());
      } catch (err) {
        setError(err.message || `Failed to load the ${providerName} integration.`);
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
    },
    [api, providerKey, providerName]
  );

  useEffect(() => {
    load();
  }, [load]);

  // Manual refresh: reload everything, bypassing the server-side read caches,
  // and tell the form editor to re-read the provider too.
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
        const res = await api.listAutomationRuns(50);
        setRuns(res?.items || []);
      } catch {
        /* transient — next poll retries */
      }
    }, 10000);
    return () => clearInterval(timer);
  }, [hasActiveRun, api]);

  const refreshRuns = async () => {
    try {
      const res = await api.listAutomationRuns(50);
      setRuns(res?.items || []);
    } catch {
      /* best-effort */
    }
  };

  const runTicketAutomation = async (ticket) => {
    setError("");
    setNotice("");
    setStartingTicketId(ticket.id);
    try {
      await api.startAutomationRun(ticket.id);
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
      await api.cancelAutomationRun(run.id);
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
      await api.deleteInboundTicket(ticket.id);
      setTickets((prev) => prev.filter((t) => t.id !== ticket.id));
    } catch (err) {
      setError(err.message || "Failed to delete the inbound ticket.");
    } finally {
      setDeletingTicketId(null);
    }
  };

  const refreshPreview = async () => {
    try {
      setPreview(await api.getPreview());
    } catch {
      /* preview is best-effort */
    }
  };

  const runTest = async () => {
    setTesting(true);
    setError("");
    setNotice("");
    try {
      const result = await api.testConnection();
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
      const result = await api.syncNow();
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

  const backButton = (
    <button type="button" className="btn-ghost sg-zh-back" onClick={onBack}>
      <IconArrowLeft />
      All providers
    </button>
  );

  if (loading) {
    return (
      <div className="zoho-page">
        <header className="sg-zh-cmdbar">
          <div>
            {backButton}
            <h2 className="sg-zh-cmdtitle">{providerName}</h2>
          </div>
        </header>
        <p className="muted">Loading…</p>
      </div>
    );
  }

  const enabled = Boolean(config?.enabled);
  const nextSync = minutesToNextSync(config);
  // Which config key names the account/site, per provider — the subtitle's
  // "connected to what" line.
  const scopeLabel = config?.orgId
    ? { label: "Desk org", value: config.orgId }
    : config?.projectKey
    ? { label: "Project", value: config.projectKey }
    : null;

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
          {backButton}
          <h2 className="sg-zh-cmdtitle">
            {providerName}
            <span className={`status-pill ${enabled ? "ok" : "muted"}`}>
              {enabled ? "On" : "Off"}
            </span>
          </h2>
          <p className="sg-zh-cmdsub">
            {scopeLabel ? (
              <>
                {scopeLabel.label} <span className="mono">{scopeLabel.value}</span>
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
            <span className="sg-zh-cmdsep">·</span>
            {/* The Settings tab used to be right here, so say where it went
                rather than leaving someone hunting for it. */}
            <span className="muted">connection settings in Settings → Integrations</span>
          </p>
        </div>
        <div className="sg-zh-cmdactions">
          <button
            type="button"
            className="btn-ghost sg-zh-copy"
            onClick={runRefresh}
            disabled={refreshing}
            title={`Re-read the config, live deployments, ${providerName} ${formNoun} and ticket log`}
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
      <nav className="sg-zh-subtabs" role="tablist" aria-label={`${providerName} integration sections`}>
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
          // A form edit can change which fields the sync publishes into, so
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

    </div>
  );
}
