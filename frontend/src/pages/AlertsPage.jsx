import { lazy, Suspense, useMemo, useState } from "react";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import { isNamespaceScopeLoading, SCOPE_LOADING_HINT } from "../utils/accessViewState.js";
import InfoCard from "../components/common/InfoCard.jsx";
import {
  buildAlertsScopeSummary,
  formatAlertTime,
  formatTriggeredConditions,
  getAlertPolicyLabel,
  getAlertResourceName,
  getAlertTypeLabel,
  hasAlertMonitoringScope,
  isLogAlert,
} from "../lib/alertDisplay.js";
import { EMPTY_MESSAGES, isAccessDeniedError } from "../utils/authz.js";

const AlertLogContextModal = lazy(() => import("../components/alerts/AlertLogContextModal.jsx"));

function IconAlertTriangle(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

function IconBell(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}

function IconHistory(props) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15 14" />
    </svg>
  );
}

/* Display-only severity mapping: critical → danger, warning → warn, everything else → info. */
function severityInfo(severity) {
  const value = String(severity || "info").toLowerCase();
  if (value === "critical") {
    return { rank: 0, tone: "danger", label: value };
  }
  if (value === "warning") {
    return { rank: 1, tone: "warn", label: value };
  }
  return { rank: 2, tone: "info", label: value };
}

function formatFiringDuration(firedAt) {
  if (!firedAt) {
    return "";
  }
  const ts = Date.parse(firedAt);
  if (Number.isNaN(ts)) {
    return "";
  }
  const totalMinutes = Math.floor((Date.now() - ts) / 60000);
  if (totalMinutes < 0) {
    return "";
  }
  if (totalMinutes < 1) {
    return "under 1 min";
  }
  if (totalMinutes < 60) {
    return `${totalMinutes} min`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours < 24) {
    return minutes ? `${hours} h ${String(minutes).padStart(2, "0")}` : `${hours} h`;
  }
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours ? `${days} d ${remHours} h` : `${days} d`;
}

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
            <dd>
              <span className="alerts-scope-tags">
                {scope.namespaces.map((ns) => (
                  <span key={ns} className="sg-tag">
                    {ns}
                  </span>
                ))}
              </span>
            </dd>
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
            <dd>
              <span className="alerts-scope-tags">
                {scope.resources.map((resource) => (
                  <span key={resource} className="sg-tag">
                    {resource}
                  </span>
                ))}
              </span>
            </dd>
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
  const [selectedAlert, setSelectedAlert] = useState(null);
  const [logModalOpen, setLogModalOpen] = useState(false);

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

  const openLogContext = (alert) => {
    setSelectedAlert(alert);
    setLogModalOpen(true);
  };

  /* Severity KPI counts — computed from the already-loaded list, no extra fetches. */
  const severityCounts = useMemo(() => {
    const counts = { critical: 0, warning: 0, info: 0 };
    for (const alert of alerts) {
      const value = String(alert.severity || "").toLowerCase();
      if (value === "critical") {
        counts.critical += 1;
      } else if (value === "warning") {
        counts.warning += 1;
      } else {
        counts.info += 1;
      }
    }
    return counts;
  }, [alerts]);

  /* Display-level ordering: severity first (critical → warning → info), then age (newest fired first). */
  const displayAlerts = useMemo(() => {
    const decorated = alerts.map((alert) => {
      const severity = severityInfo(alert.severity);
      const firedTs = Date.parse(alert.firedAt);
      const resource = getAlertResourceName(alert);
      const policy = getAlertPolicyLabel(alert);
      const status = String(alert.status || "active").toLowerCase();
      const duration = formatFiringDuration(alert.firedAt);

      const metaParts = [getAlertTypeLabel(alert).toLowerCase(), scope.clusterLabel];
      if (alert.namespace) {
        metaParts.push(`ns/${alert.namespace}`);
      }
      if (resource && resource !== "—") {
        metaParts.push(resource);
      }
      if (policy && policy !== "—") {
        metaParts.push(policy);
      }
      if (isLogAlert(alert) && alert.matchedPattern) {
        metaParts.push(`pattern ${alert.matchedPattern}`);
      }
      if (status === "active") {
        metaParts.push(duration ? `firing ${duration}` : `fired ${formatAlertTime(alert.firedAt)}`);
      } else {
        metaParts.push(alert.status);
        metaParts.push(`fired ${formatAlertTime(alert.firedAt)}`);
      }

      return {
        alert,
        severity,
        firedTs: Number.isNaN(firedTs) ? 0 : firedTs,
        summary: alert.title || formatTriggeredConditions(alert) || alert.description || "—",
        metaLine: metaParts.filter(Boolean).join(" · "),
        firedAtLabel: formatAlertTime(alert.firedAt),
      };
    });
    decorated.sort((a, b) => a.severity.rank - b.severity.rank || b.firedTs - a.firedTs);
    return decorated;
  }, [alerts, scope.clusterLabel]);

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

  let headerSubtitle;
  if (!hasClusters) {
    headerSubtitle = "Monitor workload alerts within your assigned scope.";
  } else if (!showAlertsContent) {
    headerSubtitle = `Monitoring ${scope.clusterLabel}`;
  } else {
    const parts = [`${alerts.length} open`, `monitoring ${scope.clusterLabel}`];
    if (scope.namespaces.length) {
      parts.push(`${scope.namespaces.length} namespace${scope.namespaces.length === 1 ? "" : "s"}`);
    }
    headerSubtitle = parts.join(" · ");
  }

  return (
    <div className="ops-page alerts-page">
      <header className="sg-ph">
        <div>
          <h2>Alerts</h2>
          <p className="sg-ph-sub">{headerSubtitle}</p>
        </div>
        {showAlertsContent && hasAlerts && onNavigateToAlertPolicies ? (
          <div className="sg-ph-actions">
            <button type="button" className="btn-outline" onClick={() => onNavigateToAlertPolicies("history")}>
              <IconHistory />
              View alert history
            </button>
          </div>
        ) : null}
      </header>

      {gateContent}

      {showAlertsContent && hasAlerts ? (
        <div className="sg-kpi-grid sg-alerts-kpis">
          <div className="sg-kpi sg-alerts-kpi--critical">
            <p className="sg-kpi-label">
              <IconAlertTriangle />
              Critical
            </p>
            <div className="sg-kpi-value">
              <b>{severityCounts.critical}</b>
            </div>
          </div>
          <div className="sg-kpi sg-alerts-kpi--warning">
            <p className="sg-kpi-label">
              <IconAlertTriangle />
              Warning
            </p>
            <div className="sg-kpi-value">
              <b>{severityCounts.warning}</b>
            </div>
          </div>
          <div className="sg-kpi">
            <p className="sg-kpi-label">
              <IconBell />
              Info
            </p>
            <div className="sg-kpi-value">
              <b>{severityCounts.info}</b>
            </div>
          </div>
        </div>
      ) : null}

      {showAlertsContent && hasAlerts ? (
        <section className="card compact sg-alerts-card">
          <div className="sg-alerts-card-head">
            <h3>Open alerts</h3>
            <span className="sg-alerts-card-sub">severity first, then age</span>
          </div>
          <div className="sg-alist">
            {displayAlerts.map(({ alert, severity, summary, metaLine, firedAtLabel }) => (
              <div
                key={alert.id}
                className={`sg-al${severity.tone === "danger" ? " sg-al--critical" : ""}`}
              >
                <span className={`status-pill ${severity.tone}`}>{severity.label}</span>
                <div className="sg-al-grow">
                  <b>{summary}</b>
                  <span title={firedAtLabel}>{metaLine}</span>
                </div>
                {isLogAlert(alert) ? (
                  <div className="sg-al-actions">
                    <button type="button" className="sg-al-btn" onClick={() => openLogContext(alert)}>
                      View Log Context
                    </button>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {showAlertsContent && !hasAlerts ? (
        <section className="card alerts-empty-card">
          <h3>No active alerts in your assigned resources</h3>
          <p className="muted">Everything you have access to is currently operating normally.</p>
        </section>
      ) : null}

      {showAlertsContent ? <AlertScopeCard scope={scope} /> : null}

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

      {logModalOpen ? (
        <Suspense fallback={null}>
          <AlertLogContextModal
            open={logModalOpen}
            alert={selectedAlert}
            onClose={() => {
              setLogModalOpen(false);
              setSelectedAlert(null);
            }}
          />
        </Suspense>
      ) : null}
    </div>
  );
}
