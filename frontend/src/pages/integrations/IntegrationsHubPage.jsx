import { useMemo } from "react";
import { Link } from "react-router-dom";
import PageHeader from "../../components/common/PageHeader.jsx";
import AccessScopeView from "../../components/common/AccessScopeView.jsx";
import StatusPill from "../../components/common/StatusPill.jsx";
import FreshnessIndicator from "../../components/common/FreshnessIndicator.jsx";
import { groupByCategory } from "../../lib/integrations.js";
import { useIntegrationList } from "../../hooks/useIntegrations.js";
import { pathForPageKey } from "../../routes/paths.js";

/**
 * The integrations hub.
 *
 * One address for every outside system KubeSight talks to. Connections used to
 * be configured wherever they happened to be used — registries had a sidebar
 * entry, SMTP hid behind an admin-only tab on the Alerts page, Jira and Zoho
 * were configured from inside the Ticketing workspace — so "where do I
 * configure X" had a different answer per X, and "which of these is broken" had
 * no answer at all short of opening each one.
 *
 * The provider list comes entirely from the backend. Nothing here knows that
 * Jira exists: contract 2 says render what the array contains, which is also
 * what makes the per-user filtering work — a user with `registries:view` and
 * nothing else gets a hub with exactly one card, and the frontend never had to
 * be told why.
 */

function statusCounts(items) {
  return items.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});
}

/**
 * The line an operator reads first. Leads with what is wrong, because a hub of
 * nine healthy integrations and one degraded one should not read as "10
 * integrations".
 */
function HealthSummary({ items }) {
  const counts = statusCounts(items);
  const parts = [];
  if (counts.degraded) parts.push(`${counts.degraded} degraded`);
  if (counts.disabled) parts.push(`${counts.disabled} disabled`);
  if (counts.not_configured) parts.push(`${counts.not_configured} not configured`);
  if (counts.connected) parts.push(`${counts.connected} connected`);

  if (!parts.length) {
    return null;
  }
  return (
    <p className={`sg-int-health${counts.degraded ? " is-warn" : ""}`}>{parts.join(" · ")}</p>
  );
}

function IntegrationCard({ integration }) {
  const href = pathForPageKey("integrationDetail", { provider: integration.key });

  return (
    <Link to={href} className="sg-int-card">
      <div className="sg-int-card-head">
        <span className="sg-int-card-name">{integration.name}</span>
        <StatusPill status={integration.status} />
      </div>
      <p className="sg-int-card-message muted">{integration.message}</p>
      <div className="sg-int-card-foot">
        {/*
          Which timestamp matters depends on the provider: a ticketing
          integration is judged on its last successful sync, one that only has a
          connection check on its last test. Show whichever the backend
          populated rather than inventing a single "last activity".
        */}
        {integration.lastSuccessfulSyncAt ? (
          <FreshnessIndicator timestamp={integration.lastSuccessfulSyncAt} prefix="Synced" />
        ) : integration.lastTestedAt ? (
          <FreshnessIndicator timestamp={integration.lastTestedAt} prefix="Tested" />
        ) : (
          <span className="muted">Never checked</span>
        )}
      </div>
    </Link>
  );
}

export default function IntegrationsHubPage() {
  const { items, loading, error } = useIntegrationList();
  const groups = useMemo(() => groupByCategory(items), [items]);

  return (
    <>
      <PageHeader
        pageKey="integrations"
        title="Integrations"
        subtitle="Every outside system KubeSight connects to, and whether it is working."
        meta={items.length ? <HealthSummary items={items} /> : null}
      />

      <AccessScopeView
        pageLoading={loading}
        accessError={error}
        empty={!items.length}
        loadingLabel="Loading integrations..."
        emptyMessage="No integrations are available to your account."
        emptyHint="Integrations you may configure appear here. Contact an administrator if you expected one."
      >
        <div className="sg-int-groups">
          {groups.map((group) => (
            <section key={group.category} className="sg-int-group">
              <h3 className="sg-int-group-title">{group.category}</h3>
              <div className="sg-int-grid">
                {group.items.map((integration) => (
                  <IntegrationCard key={integration.key} integration={integration} />
                ))}
              </div>
            </section>
          ))}
        </div>
      </AccessScopeView>
    </>
  );
}
