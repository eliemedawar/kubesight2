import { useCallback, useEffect, useState } from "react";
import ErrorBanner from "../../components/common/ErrorBanner.jsx";
import LoadingState from "../../components/common/LoadingState.jsx";
import EmptyState from "../../components/common/EmptyState.jsx";
import {
  getIntegration,
  listIntegrationActivity,
  setIntegrationEnabled,
  testIntegration,
} from "../../api/integrationsApi.js";
import {
  capabilityLabel,
  DETAIL_TABS,
  formatTimestamp,
  hasAction,
  statusMeta,
  timeAgo,
} from "../../lib/integrations.js";
import ICONS from "./settingsIcons.jsx";
import ConfigurationPanel from "./integrationConfigPanels.jsx";

/**
 * One integration, four rooms.
 *
 *   Overview       is it working, since when, and what does it do for us
 *   Configuration  the provider's own form — reused, not reimplemented
 *   Activity       what it has actually done lately
 *   Used by        what breaks here if this connection goes away
 *
 * Every integration gets all four tabs even when one is thin: a consistent
 * shape is what lets an operator learn the screen once. A tab with nothing in
 * it says so rather than disappearing, because a missing tab reads as "this
 * integration is different" when the truth is "nothing has happened yet".
 */

function StatusPill({ status }) {
  const meta = statusMeta(status);
  return (
    <span className={`status-pill ${meta.tone}`} title={meta.hint}>
      {meta.label}
    </span>
  );
}

function OverviewTab({ integration, onTest, testing, testResult }) {
  const meta = statusMeta(integration.status);
  return (
    <div className="sg-int-overview">
      <section className="card sg-int-state">
        <div className="sg-int-state-main">
          <StatusPill status={integration.status} />
          <p className="sg-int-state-msg">{integration.message || meta.hint}</p>
        </div>
        {hasAction(integration, "test") ? (
          <button type="button" className="btn-outline" onClick={onTest} disabled={testing}>
            {testing ? "Testing…" : "Test connection"}
          </button>
        ) : null}
      </section>

      {testResult ? (
        <p className={`sg-int-testresult ${testResult.ok ? "is-ok" : "is-bad"}`} role="status">
          {testResult.message}
        </p>
      ) : null}

      <dl className="sg-int-facts">
        <div>
          <dt>Last tested</dt>
          <dd title={formatTimestamp(integration.lastTestedAt)}>
            {integration.lastTestedAt ? timeAgo(integration.lastTestedAt) : "Never"}
          </dd>
        </div>
        <div>
          <dt>Last successful sync</dt>
          <dd title={formatTimestamp(integration.lastSuccessfulSyncAt)}>
            {integration.lastSuccessfulSyncAt
              ? timeAgo(integration.lastSuccessfulSyncAt)
              : "Never"}
          </dd>
        </div>
        <div>
          <dt>Category</dt>
          <dd>{integration.category}</dd>
        </div>
      </dl>

      <section className="card sg-int-caps-card">
        <h4>What it does</h4>
        {integration.capabilities?.length ? (
          <ul className="sg-int-caplist">
            {integration.capabilities.map((capability) => (
              <li key={capability}>{capabilityLabel(capability)}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">No capabilities reported for this integration.</p>
        )}
      </section>
    </div>
  );
}

function ActivityTab({ integrationKey, integrationName }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await listIntegrationActivity(integrationKey);
      setEntries(response?.items || []);
    } catch (err) {
      setError(err.message || "Failed to load activity.");
    } finally {
      setLoading(false);
    }
  }, [integrationKey]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return <LoadingState label="Loading activity..." />;
  }

  return (
    <div className="sg-int-activity">
      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      <div className="sg-int-activity-head">
        <p className="muted">The most recent things {integrationName} has done.</p>
        <button type="button" className="btn-ghost" onClick={load}>
          Refresh
        </button>
      </div>
      {entries.length ? (
        <ol className="sg-int-timeline">
          {entries.map((entry) => (
            <li key={entry.id} className={`sg-int-event is-${entry.outcome || "info"}`}>
              <span className="sg-int-event-dot" aria-hidden="true" />
              <span className="sg-int-event-body">
                <span className="sg-int-event-title">{entry.summary}</span>
                {entry.detail ? <span className="sg-int-event-detail">{entry.detail}</span> : null}
              </span>
              <span className="sg-int-event-when" title={formatTimestamp(entry.at)}>
                {timeAgo(entry.at)}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <EmptyState
          title="Nothing yet"
          message={`${integrationName} has not recorded any activity. Entries appear here once it runs.`}
        />
      )}
    </div>
  );
}

function UsedByTab({ integration }) {
  const usedBy = integration.usedBy || [];
  if (!usedBy.length) {
    return (
      <EmptyState
        title="Not referenced yet"
        message={`Nothing in KubeSight depends on ${integration.name} right now. Turning it off would have no effect.`}
      />
    );
  }
  return (
    <div className="sg-int-usedby">
      <p className="muted">
        These stop working if {integration.name} is disabled or its connection breaks.
      </p>
      <ul className="sg-int-usedby-list">
        {usedBy.map((entry) => {
          const label = typeof entry === "string" ? entry : entry.label;
          const detail = typeof entry === "string" ? null : entry.detail;
          return (
            <li key={label}>
              <span className="sg-int-usedby-label">{label}</span>
              {detail ? <span className="sg-int-usedby-detail">{detail}</span> : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default function IntegrationDetail({
  integration: initial,
  hasPermission = () => false,
  isAdmin = false,
  onBack,
  onChanged,
}) {
  const [integration, setIntegration] = useState(initial);
  const [tab, setTab] = useState("overview");
  const [error, setError] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [togglingEnabled, setTogglingEnabled] = useState(false);

  // The list endpoint returns enough for a card; the detail endpoint may carry
  // more (config summary, richer usedBy). Refresh into the fuller shape.
  const refresh = useCallback(async () => {
    try {
      const fresh = await getIntegration(initial.key);
      if (fresh) setIntegration(fresh);
    } catch (err) {
      setError(err.message || "Failed to reload this integration.");
    }
  }, [initial.key]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    setError("");
    try {
      const result = await testIntegration(integration.key);
      setTestResult({
        ok: result?.ok !== false,
        message: result?.message || (result?.ok === false ? "Test failed." : "Connection succeeded."),
      });
      await refresh();
      onChanged?.();
    } catch (err) {
      setTestResult({ ok: false, message: err.message || "Test failed." });
    } finally {
      setTesting(false);
    }
  };

  const toggleEnabled = async () => {
    const next = !integration.enabled;
    setTogglingEnabled(true);
    setError("");
    try {
      await setIntegrationEnabled(integration.key, next);
      await refresh();
      onChanged?.();
    } catch (err) {
      setError(err.message || "Failed to change this integration.");
    } finally {
      setTogglingEnabled(false);
    }
  };

  const canDisable = hasAction(integration, "disable") || hasAction(integration, "enable");

  return (
    <div className="sg-int-detail">
      <header className="sg-int-detail-head">
        <button type="button" className="btn-ghost sg-int-back" onClick={onBack}>
          <span className="sg-int-back-icon" aria-hidden="true">
            {ICONS.chevron}
          </span>
          All integrations
        </button>
        <div className="sg-int-detail-title">
          <h3>{integration.name}</h3>
          <StatusPill status={integration.status} />
        </div>
        {canDisable ? (
          <button
            type="button"
            className="btn-outline"
            onClick={toggleEnabled}
            disabled={togglingEnabled}
          >
            {togglingEnabled ? "Saving…" : integration.enabled ? "Disable" : "Enable"}
          </button>
        ) : null}
      </header>

      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}

      <div className="sg-int-tabs" role="tablist" aria-label={`${integration.name} sections`}>
        {DETAIL_TABS.map((entry) => (
          <button
            key={entry.key}
            type="button"
            role="tab"
            aria-selected={tab === entry.key}
            className={`sg-int-tab${tab === entry.key ? " is-on" : ""}`}
            onClick={() => setTab(entry.key)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <div className="sg-int-tabpanel">
        {tab === "overview" ? (
          <OverviewTab
            integration={integration}
            onTest={runTest}
            testing={testing}
            testResult={testResult}
          />
        ) : null}
        {tab === "configuration" ? (
          <ConfigurationPanel
            integration={integration}
            hasPermission={hasPermission}
            isAdmin={isAdmin}
            onChanged={() => {
              refresh();
              onChanged?.();
            }}
          />
        ) : null}
        {tab === "activity" ? (
          <ActivityTab integrationKey={integration.key} integrationName={integration.name} />
        ) : null}
        {tab === "usedBy" ? <UsedByTab integration={integration} /> : null}
      </div>
    </div>
  );
}
