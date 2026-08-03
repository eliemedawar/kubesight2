import { useCallback, useEffect, useState } from "react";
import { listTicketingProviders } from "../api/ticketingApi.js";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import ProviderWorkspace from "../components/ticketing/ProviderWorkspace.jsx";
import { TicketingProvider } from "../components/ticketing/TicketingContext.jsx";
import { IconChevronRight, IconPlug, IconRefresh } from "../components/zoho/icons.jsx";
import { timeAgo } from "../components/zoho/common.jsx";

// Ticketing: pick a provider, then work in it.
//
// The landing view is the two cards; picking one mounts <ProviderWorkspace>
// inside a <TicketingProvider> so everything below it is bound to that provider
// and never has to know which one it is.
//
// The chosen provider is remembered in localStorage, not the URL: this page is
// reached through the app's own nav state (there is no router), so a per-browser
// memory is what makes "open Ticketing" land back where the operator left off.
const LAST_PROVIDER_KEY = "kubesight.ticketing.provider";

function readLastProvider() {
  try {
    return window.localStorage.getItem(LAST_PROVIDER_KEY) || "";
  } catch {
    return "";
  }
}

function rememberProvider(key) {
  try {
    if (key) window.localStorage.setItem(LAST_PROVIDER_KEY, key);
    else window.localStorage.removeItem(LAST_PROVIDER_KEY);
  } catch {
    /* private mode — the picker simply doesn't remember */
  }
}

/** Status line under a card: what an operator needs before clicking Configure. */
function cardStatus(provider) {
  if (!provider.configured) {
    return { tone: "muted", pill: "Not configured", detail: "No connection details saved yet." };
  }
  if (!provider.enabled) {
    return {
      tone: "warn",
      pill: "Configured, off",
      detail: "Connection saved but the integration is disabled — no sync runs.",
    };
  }
  if (provider.lastSyncStatus === "error") {
    return {
      tone: "danger",
      pill: "Last sync failed",
      detail: provider.lastSyncMessage || "The most recent sync reported an error.",
    };
  }
  if (provider.lastSyncStatus === "ok") {
    return {
      tone: "ok",
      pill: "Connected",
      detail: provider.lastSyncAt
        ? `Last synced ${timeAgo(provider.lastSyncAt)}.`
        : "Connected and enabled.",
    };
  }
  return { tone: "info", pill: "Enabled", detail: "Enabled but has not synced yet." };
}

function ProviderCard({ provider, canManage, onOpen }) {
  const status = cardStatus(provider);
  return (
    <button type="button" className="sg-tk-card" onClick={() => onOpen(provider.key)}>
      <span className="sg-tk-card-head">
        <span className="sg-tk-card-icon" aria-hidden="true">
          <IconPlug />
        </span>
        <span className="sg-tk-card-name">
          <b>{provider.name}</b>
          <span className="muted">{provider.tagline}</span>
        </span>
        <span className={`status-pill ${status.tone}`}>{status.pill}</span>
      </span>

      <span className="sg-tk-card-detail">{status.detail}</span>

      <span className="sg-tk-card-foot">
        <span className="sg-tk-card-cta">
          {canManage ? "Configure" : "View"}
          <IconChevronRight />
        </span>
      </span>
    </button>
  );
}

export default function TicketingPage({ canManage = false, embedded = false }) {
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(() => readLastProvider());

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await listTicketingProviders();
      setProviders(res?.items || []);
    } catch (err) {
      setError(err.message || "Failed to load the ticketing providers.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const open = (key) => {
    rememberProvider(key);
    setSelected(key);
  };

  const back = () => {
    rememberProvider("");
    setSelected("");
    // The cards show each provider's last sync, which the workspace may have
    // just changed — re-read rather than showing a stale card.
    load();
  };

  const active = providers.find((p) => p.key === selected);

  if (selected && active) {
    return (
      <TicketingProvider descriptor={active}>
        <ProviderWorkspace canManage={canManage} onBack={back} />
      </TicketingProvider>
    );
  }

  return (
    <div className="zoho-page sg-tk-page">
      <header className="sg-zh-cmdbar">
        {/* Embedded in Settings → Integrations the shell has already titled the
            panel, so only the actions survive. */}
        {embedded ? (
          <div />
        ) : (
          <div>
            <h2 className="sg-zh-cmdtitle">Ticketing</h2>
            <p className="sg-zh-cmdsub">
              Connect KubeSight to your ticketing platform. Deployment tickets flow in, Jenkins
              deploys, and the outcome is written back onto the ticket.
            </p>
          </div>
        )}
        <div className="sg-zh-cmdactions">
          <button
            type="button"
            className="btn-ghost sg-zh-copy"
            onClick={load}
            disabled={loading}
            title="Re-read each provider's status"
          >
            <IconRefresh className={loading ? "sg-zh-spin" : undefined} />
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}

      {loading && providers.length === 0 ? (
        <p className="muted">Loading…</p>
      ) : (
        <div className="sg-tk-grid">
          {providers.map((provider) => (
            <ProviderCard
              key={provider.key}
              provider={provider}
              canManage={canManage}
              onOpen={open}
            />
          ))}
        </div>
      )}

      <p className="muted sg-tk-note">
        Both providers share one deploy surface — the source cluster, its namespaces, the custom
        environments and the Jenkins router are configured once and used by whichever platform
        raised the ticket.
      </p>
    </div>
  );
}
