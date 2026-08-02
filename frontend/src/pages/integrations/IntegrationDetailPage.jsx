import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { NavLink, useParams } from "react-router-dom";
import PageHeader from "../../components/common/PageHeader.jsx";
import AccessScopeView from "../../components/common/AccessScopeView.jsx";
import LoadingState from "../../components/common/LoadingState.jsx";
import StatusPill from "../../components/common/StatusPill.jsx";
import FreshnessIndicator from "../../components/common/FreshnessIndicator.jsx";
import ActivityTimeline from "../../components/common/ActivityTimeline.jsx";
import EmptyState from "../../components/common/EmptyState.jsx";
import { ACTION_LABELS, capabilityLabel, hasAction } from "../../lib/integrations.js";
import { listIntegrationActivity } from "../../api/integrationsApi.js";
import { useIntegration, useIntegrationActions } from "../../hooks/useIntegrations.js";
import { pathForPageKey } from "../../routes/paths.js";

const ConfigurationPanel = lazy(() => import("../settings/integrationConfigPanels.jsx"));

/**
 * One integration: Overview, Configuration, Activity, Used by.
 *
 * The tabs are routes rather than component state, so an operator can send a
 * colleague the Activity tab of a failing provider instead of the hub plus a
 * description of where to click. That is the same reason the rest of this track
 * exists.
 *
 * Every control on this screen comes from the `actions` array. Nothing is
 * computed client-side — contract 2 is explicit that the array is the subset of
 * actions *this user* may perform, already resolved against their permissions
 * server-side. Re-deriving it here would mean two authorization models that can
 * disagree, and the one in the browser is the one that is wrong.
 */

const TABS = [
  { pageKey: "integrationDetail", label: "Overview" },
  { pageKey: "integrationConfiguration", label: "Configuration" },
  { pageKey: "integrationActivity", label: "Activity" },
  { pageKey: "integrationUsedBy", label: "Used by" },
];

function Facts({ integration }) {
  return (
    <dl className="sg-int-facts">
      <div>
        <dt>Category</dt>
        <dd>{integration.category || "—"}</dd>
      </div>
      <div>
        <dt>Last tested</dt>
        <dd>
          <FreshnessIndicator timestamp={integration.lastTestedAt} prefix="" />
        </dd>
      </div>
      <div>
        <dt>Last successful sync</dt>
        <dd>
          {/*
            Providers that have no sync concept report null here forever, which
            is a fact about the provider rather than a fault. "Never" is the
            honest rendering; a blank cell reads as a bug.
          */}
          <FreshnessIndicator timestamp={integration.lastSuccessfulSyncAt} prefix="" />
        </dd>
      </div>
    </dl>
  );
}

function OverviewTab({ integration, actions }) {
  const { testing, testResult, toggling, actionError, runTest, setEnabled } = actions;
  const canTest = hasAction(integration, "test");
  const canEnable = hasAction(integration, "enable");
  const canDisable = hasAction(integration, "disable");

  return (
    <div className="sg-int-overview">
      <section className="card">
        <div className="sg-int-status-row">
          <StatusPill status={integration.status} />
          <p className="sg-int-message">{integration.message}</p>
        </div>

        <Facts integration={integration} />

        <div className="sg-int-actions">
          {canTest ? (
            <button type="button" className="btn-outline" onClick={runTest} disabled={testing}>
              {/*
                Testing reaches an external host and contract 2 says not to time
                out under 30s, so this is a pending state rather than a spinner
                with a deadline.
              */}
              {testing ? "Testing — this can take a moment…" : ACTION_LABELS.test}
            </button>
          ) : null}
          {canDisable ? (
            <button
              type="button"
              className="btn-outline"
              onClick={() => setEnabled(false)}
              disabled={toggling}
            >
              {ACTION_LABELS.disable}
            </button>
          ) : null}
          {canEnable ? (
            <button
              type="button"
              className="primary"
              onClick={() => setEnabled(true)}
              disabled={toggling}
            >
              {ACTION_LABELS.enable}
            </button>
          ) : null}
        </div>

        {testResult ? (
          <p
            className={`sg-int-test-result${testResult.ok ? " is-ok" : " is-error"}`}
            role="status"
          >
            {testResult.message}
          </p>
        ) : null}
        {actionError ? (
          <p className="form-error" role="alert">
            {actionError}
          </p>
        ) : null}
      </section>

      <section className="card">
        <h3>Capabilities</h3>
        {integration.capabilities?.length ? (
          <ul className="sg-int-caps">
            {integration.capabilities.map((slug) => (
              <li key={slug}>{capabilityLabel(slug)}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">This integration declares no capabilities.</p>
        )}
      </section>
    </div>
  );
}

function ActivityTab({ providerKey }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    listIntegrationActivity(providerKey)
      .then((response) => {
        if (!cancelled) {
          setEntries(response?.items || []);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message || "Could not load activity.");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [providerKey]);

  return (
    <section className="card">
      <AccessScopeView pageLoading={loading} accessError={error} loadingLabel="Loading activity...">
        <ActivityTimeline
          entries={entries}
          emptyMessage="No activity recorded for this integration yet."
        />
      </AccessScopeView>
    </section>
  );
}

function UsedByTab({ integration }) {
  if (!integration.usedBy?.length) {
    return (
      <section className="card">
        <EmptyState
          message="Nothing depends on this integration yet."
          hint="Features that rely on this connection will be listed here, so you can see what breaks if it is switched off."
        />
      </section>
    );
  }

  return (
    <section className="card">
      <p className="muted">
        Switching this off or losing the connection affects the following:
      </p>
      <ul className="sg-int-usedby">
        {integration.usedBy.map((entry) => (
          <li key={entry}>{entry}</li>
        ))}
      </ul>
    </section>
  );
}

export default function IntegrationDetailPage({ tab = "overview" }) {
  const { provider } = useParams();
  const { integration, loading, error, notFound, forbidden, reload } = useIntegration(provider);
  const actions = useIntegrationActions({ providerKey: provider, onChanged: reload });

  const renderTab = useCallback(() => {
    switch (tab) {
      case "configuration":
        return (
          <Suspense fallback={<LoadingState label="Loading configuration..." />}>
            <ConfigurationPanel integration={integration} onChanged={reload} />
          </Suspense>
        );
      case "activity":
        return <ActivityTab providerKey={provider} />;
      case "usedBy":
        return <UsedByTab integration={integration} />;
      default:
        return <OverviewTab integration={integration} actions={actions} />;
    }
  }, [tab, integration, actions, provider, reload]);

  // 404 and 403 read very differently to an operator: one means the link is
  // wrong, the other means the link is right and their account is not.
  if (notFound) {
    return (
      <>
        <PageHeader pageKey="integrations" title="Integration not found" />
        <EmptyState
          message={`There is no integration called “${provider}”.`}
          hint="The link may be out of date, or the provider may have been removed."
        />
      </>
    );
  }

  if (forbidden) {
    return (
      <>
        <PageHeader pageKey="integrations" title="Access restricted" />
        <section className="card access-denied">
          <p className="muted">
            You do not have access to the <strong>{provider}</strong> integration. Contact an
            administrator if you need it.
          </p>
        </section>
      </>
    );
  }

  return (
    <>
      <PageHeader
        pageKey="integrationDetail"
        params={{ provider }}
        title={integration?.name || provider}
        currentLabel={integration?.name || provider}
        subtitle={integration?.category}
        meta={integration ? <StatusPill status={integration.status} /> : null}
      />

      <nav className="sg-int-tabs" aria-label="Integration sections">
        {TABS.map((entry) => (
          <NavLink
            key={entry.pageKey}
            to={pathForPageKey(entry.pageKey, { provider })}
            end={entry.pageKey === "integrationDetail"}
            className={({ isActive }) => `sg-int-tab${isActive ? " active" : ""}`}
          >
            {entry.label}
          </NavLink>
        ))}
      </nav>

      <AccessScopeView pageLoading={loading} accessError={error} loadingLabel="Loading integration...">
        {integration ? renderTab() : null}
      </AccessScopeView>
    </>
  );
}
