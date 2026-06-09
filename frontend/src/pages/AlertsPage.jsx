import { useMemo } from "react";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import { isNamespaceScopeLoading, SCOPE_LOADING_HINT } from "../utils/accessViewState.js";
import DataTable from "../components/common/DataTable.jsx";
import InfoCard from "../components/common/InfoCard.jsx";
import {
  buildAlertsScopeSummary,
  formatAlertTime,
  formatTriggeredConditions,
  getAlertPolicyLabel,
  getAlertResourceName,
  hasAlertMonitoringScope,
} from "../lib/alertDisplay.js";
import { EMPTY_MESSAGES, isAccessDeniedError } from "../utils/authz.js";

function AlertScopeCard({ scope }) {
  if (!scope.clusterId) {
    return null;
  }
  return (
    <section className="card alerts-scope-card">
      <h3>Showing alerts for</h3>
      <dl className="alerts-scope-list">
        <div>
          <dt>Cluster</dt>
          <dd>{scope.clusterLabel}</dd>
        </div>
        {scope.namespaces.length ? (
          <div>
            <dt>Namespaces</dt>
            <dd>{scope.namespaces.join(", ")}</dd>
          </div>
        ) : (
          <div>
            <dt>Namespaces</dt>
            <dd className="muted">All namespaces you can access in this cluster</dd>
          </div>
        )}
        {scope.resources.length ? (
          <div>
            <dt>Resources</dt>
            <dd>{scope.resources.join(", ")}</dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}

export default function AlertsPage({
  data,
  selectedClusterId,
  allowedClusters,
  allowedNamespaces,
  allowedResources,
  canManageRouting,
  onNavigateToAlertRouting,
  canManageAlerts,
  hasClusters,
  authUser,
  coreLoading = false,
  namespacesLoading = false,
  accessError = "",
  onNavigateToAlertPolicies,
}) {
  const alerts = data.alerts || [];
  const hasAlerts = alerts.length > 0;
  const hasScope = hasAlertMonitoringScope({
    hasClusters,
    namespaces: allowedNamespaces,
    resources: allowedResources,
    user: authUser,
  });

  const scope = useMemo(
    () =>
      buildAlertsScopeSummary({
        clusterId: selectedClusterId,
        clusters: allowedClusters,
        namespaces: allowedNamespaces,
        resources: allowedResources,
      }),
    [selectedClusterId, allowedClusters, allowedNamespaces, allowedResources]
  );

  const subtitle = useMemo(() => {
    if (!hasClusters) {
      return "Monitor workload alerts within your assigned scope.";
    }
    return `Monitoring ${scope.clusterLabel}`;
  }, [hasClusters, scope.clusterLabel]);

  const alertRows = useMemo(
    () =>
      alerts.map((alert) => ({
        id: alert.id,
        severity: alert.severity || "—",
        policy: getAlertPolicyLabel(alert),
        resource: getAlertResourceName(alert),
        cluster: scope.clusterLabel,
        namespace: alert.namespace || "—",
        firedAt: formatAlertTime(alert.firedAt),
        status: alert.status || "—",
        summary: alert.title || formatTriggeredConditions(alert) || alert.description || "—",
        source: alert.source,
        policyId: alert.policyId,
      })),
    [alerts, scope.clusterLabel]
  );

  const alertColumns = [
    { key: "severity", label: "Severity" },
    { key: "summary", label: "Alert" },
    { key: "policy", label: "Policy" },
    { key: "resource", label: "Source resource" },
    { key: "namespace", label: "Namespace" },
    { key: "cluster", label: "Cluster" },
    { key: "firedAt", label: "Time" },
    { key: "status", label: "Status" },
  ];

  const scopeLoading = isNamespaceScopeLoading({
    coreLoading,
    namespacesLoading,
  });

  let gateContent = null;
  if (scopeLoading) {
    const scopeLabel = coreLoading ? "Loading clusters..." : "Loading namespaces...";
    gateContent = <LoadingState label={scopeLabel} hint={SCOPE_LOADING_HINT} />;
  } else if (isAccessDeniedError(accessError)) {
    gateContent = <AccessDeniedPage message={accessError} />;
  } else if (accessError) {
    gateContent = <ErrorBanner message={accessError} suppressAccessDenied={false} />;
  } else if (!hasClusters) {
    gateContent = <EmptyState message={EMPTY_MESSAGES.noClusters} />;
  } else if (!hasScope) {
    gateContent = (
      <EmptyState
        message="No resources are assigned to your account."
        hint="Contact an administrator if you believe this is incorrect."
      />
    );
  }

  const showAlertsContent = !gateContent;

  return (
    <div className="ops-page">
      <PageTitle title="Alerts" subtitle={subtitle} />

      {gateContent}

      {showAlertsContent ? <AlertScopeCard scope={scope} /> : null}

      {showAlertsContent && hasAlerts ? (
        <section className="ops-section">
          <div className="card-header-row">
            <div>
              <h3>Active alerts</h3>
              <p className="muted alerts-table-meta">
                {alerts.length} alert{alerts.length === 1 ? "" : "s"} in your assigned scope
              </p>
            </div>
            {onNavigateToAlertPolicies ? (
              <button type="button" className="btn-outline" onClick={() => onNavigateToAlertPolicies("history")}>
                View alert history
              </button>
            ) : null}
          </div>
          <DataTable columns={alertColumns} rows={alertRows} />
        </section>
      ) : null}

      {showAlertsContent && !hasAlerts ? (
        <section className="card alerts-empty-card">
          <h3>No active alerts in your assigned resources</h3>
          <p className="muted">Everything you have access to is currently operating normally.</p>
        </section>
      ) : null}

      {showAlertsContent && canManageRouting ? (
        <InfoCard
          title="Notification Channels"
          actionLabel="Configure routing"
          onAction={onNavigateToAlertRouting}
        >
          <p className="muted">
            Manage SMTP and notification receivers in Administration → Alert Routing. Assign receivers on each Alert Policy.
          </p>
        </InfoCard>
      ) : null}
    </div>
  );
}
