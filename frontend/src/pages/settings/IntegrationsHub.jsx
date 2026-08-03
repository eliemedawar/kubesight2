import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import ErrorBanner from "../../components/common/ErrorBanner.jsx";
import LoadingState from "../../components/common/LoadingState.jsx";
import PageTitle from "../../components/common/PageTitle.jsx";
import { listIntegrations } from "../../api/integrationsApi.js";
import { groupByCategory, statusMeta, timeAgo } from "../../lib/integrations.js";

const IntegrationDetail = lazy(() => import("./IntegrationDetail.jsx"));

/**
 * The integrations hub — every outside system KubeSight talks to, as cards.
 *
 * A card answers the only question worth asking from a distance: is this
 * working? Everything specific — which host, which credentials, what failed —
 * is one click away on the detail screen. The four states are deliberately
 * coarse (see lib/integrations.js): an operator scanning this grid is triaging,
 * not debugging.
 *
 * The grid is provider-neutral. It renders whatever `/api/integrations`
 * returns, so a new provider appears here as soon as the backend describes it.
 */

const LEAD_LINE =
  "Every outside system KubeSight connects to. Configure a connection here; what you do with it lives on its own page.";

function StatusPill({ status }) {
  const meta = statusMeta(status);
  return (
    <span className={`status-pill ${meta.tone}`} title={meta.hint}>
      {meta.label}
    </span>
  );
}

/** One line under the name: the most useful thing we know right now. */
function cardDetail(integration) {
  if (integration.message) {
    return integration.message;
  }
  if (integration.status === "not_configured") {
    return "No connection details saved yet.";
  }
  if (integration.status === "disabled") {
    return "Configured but switched off.";
  }
  if (integration.lastSuccessfulSyncAt) {
    return `Last synced ${timeAgo(integration.lastSuccessfulSyncAt)}.`;
  }
  if (integration.lastTestedAt) {
    return `Last tested ${timeAgo(integration.lastTestedAt)}.`;
  }
  return "Connected — not exercised yet.";
}

function IntegrationCard({ integration, onOpen }) {
  return (
    <button type="button" className="sg-int-card" onClick={() => onOpen(integration.key)}>
      <span className="sg-int-card-head">
        <span className="sg-int-card-name">
          <b>{integration.name}</b>
          <span className="muted">{integration.category}</span>
        </span>
        <StatusPill status={integration.status} />
      </span>

      <span className="sg-int-card-detail">{cardDetail(integration)}</span>

      {integration.capabilities?.length ? (
        <span className="sg-int-card-caps">
          {integration.capabilities.slice(0, 3).map((capability) => (
            <span className="sg-int-cap" key={capability}>
              {capability}
            </span>
          ))}
          {integration.capabilities.length > 3 ? (
            <span className="sg-int-cap sg-int-cap--more">
              +{integration.capabilities.length - 3}
            </span>
          ) : null}
        </span>
      ) : null}
    </button>
  );
}

/** A one-line count of what needs attention, above the grid. */
function HealthLine({ items }) {
  const counts = items.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
  const parts = [];
  if (counts.connected) parts.push(`${counts.connected} connected`);
  if (counts.degraded) parts.push(`${counts.degraded} degraded`);
  if (counts.disabled) parts.push(`${counts.disabled} disabled`);
  if (counts.not_configured) parts.push(`${counts.not_configured} not configured`);
  if (!parts.length) return null;
  return (
    <p className={`sg-int-health${counts.degraded ? " is-warn" : ""}`}>{parts.join(" · ")}</p>
  );
}

export default function IntegrationsHub({ hasPermission = () => false, isAdmin = false }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedKey, setSelectedKey] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await listIntegrations();
      setItems(response?.items || []);
    } catch (err) {
      setError(err.message || "Failed to load integrations.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const groups = useMemo(() => groupByCategory(items), [items]);
  const selected = items.find((item) => item.key === selectedKey) || null;

  if (selectedKey && selected) {
    return (
      <Suspense fallback={<LoadingState label={`Loading ${selected.name}...`} />}>
        <IntegrationDetail
          integration={selected}
          hasPermission={hasPermission}
          isAdmin={isAdmin}
          onBack={() => {
            setSelectedKey("");
            // The detail screen may have tested, enabled, or reconfigured the
            // integration — the card behind it would otherwise show the state
            // from before that happened.
            load();
          }}
          onChanged={load}
        />
      </Suspense>
    );
  }

  return (
    <div className="sg-int-hub">
      <div className="sg-int-head">
        <PageTitle title="Integrations" subtitle={LEAD_LINE} />
        <button type="button" className="btn-ghost" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}

      {loading && !items.length ? (
        <LoadingState label="Loading integrations..." />
      ) : (
        <>
          <HealthLine items={items} />
          {groups.map((group) => (
            <section className="sg-int-group" key={group.category}>
              <h4 className="sg-int-group-title">{group.category}</h4>
              <div className="sg-int-grid">
                {group.items.map((integration) => (
                  <IntegrationCard
                    key={integration.key}
                    integration={integration}
                    onOpen={setSelectedKey}
                  />
                ))}
              </div>
            </section>
          ))}
          {!items.length && !error ? (
            <p className="muted">No integrations are visible to your account.</p>
          ) : null}
        </>
      )}
    </div>
  );
}
