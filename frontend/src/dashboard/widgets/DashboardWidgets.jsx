import StatCard from "../../components/common/StatCard.jsx";
import InfoCard from "../../components/common/InfoCard.jsx";
import Sparkline from "../charts/Sparkline.jsx";

const IconServer = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path fillRule="evenodd" d="M2 5a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 01-2 2H4a2 2 0 01-2-2V5zm14 1a1 1 0 11-2 0 1 1 0 012 0zM2 13a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 01-2 2H4a2 2 0 01-2-2v-2zm14 1a1 1 0 11-2 0 1 1 0 012 0z" clipRule="evenodd" />
  </svg>
);

const IconHexagon = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path d="M10 1.5l7.794 4.5v9L10 19.5 2.206 15v-9L10 1.5z" fillOpacity="0.8" />
    <path d="M10 3.732L3.5 7.5v5l6.5 3.768L16.5 12.5v-5L10 3.732z" fill="none" stroke="currentColor" strokeWidth="1.2" />
  </svg>
);

const IconCpu = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path fillRule="evenodd" d="M6 2a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V4a2 2 0 00-2-2H6zm1 2a1 1 0 000 2h6a1 1 0 100-2H7zm6 7a1 1 0 011 1v3a1 1 0 11-2 0v-3a1 1 0 011-1zm-3 3a1 1 0 100 2h.01a1 1 0 100-2H10zm-4-1a1 1 0 011 1v3a1 1 0 11-2 0v-3a1 1 0 011-1z" clipRule="evenodd" />
  </svg>
);

const IconMemory = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path d="M3 4a1 1 0 000 2h14a1 1 0 100-2H3zM3 9a1 1 0 000 2h14a1 1 0 100-2H3zM3 14a1 1 0 000 2h14a1 1 0 100-2H3z" />
  </svg>
);

const IconNodes = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path d="M13 6a3 3 0 11-6 0 3 3 0 016 0zM18 8a2 2 0 11-4 0 2 2 0 014 0zM14 15a4 4 0 00-8 0v1h8v-1zM6 8a2 2 0 11-4 0 2 2 0 014 0zM16 18v-1a5.972 5.972 0 00-.75-2.906A3.005 3.005 0 0119 15v1h-3zM4.75 14.094A5.973 5.973 0 004 17v1H1v-1a3 3 0 013.75-2.906z" />
  </svg>
);

const IconPods = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" />
  </svg>
);

const IconApps = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path d="M5 3a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2H5zM5 11a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2v-2a2 2 0 00-2-2H5zM11 5a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V5zM11 13a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
  </svg>
);

const IconAlertCircle = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
  </svg>
);

const IconShieldExclaim = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path fillRule="evenodd" d="M2.166 4.999A11.954 11.954 0 0010 1.944 11.954 11.954 0 0017.834 5c.11.65.166 1.32.166 2.001 0 5.225-3.34 9.67-8 11.317C5.34 16.67 2 12.225 2 7c0-.682.057-1.35.166-2.001zm5.834 4.001a1 1 0 00-1 1v2a1 1 0 002 0v-2a1 1 0 00-1-1zm0 6a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
  </svg>
);

const IconChartBar = () => (
  <svg viewBox="0 0 20 20" fill="currentColor" width="16" height="16" aria-hidden="true">
    <path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z" />
  </svg>
);
import {
  formatDashboardTime,
  formatLatestVersion,
  formatUtilizationValue,
  HEALTH_ICONS,
  HEALTH_LABELS,
  statusTone,
  utilizationIcon,
  utilizationTone,
  versionStatusDisplay,
  versionStatusTone,
} from "../../utils/dashboardStatus.js";

function StatusBadge({ status, label }) {
  const tone = statusTone(status);
  return (
    <span className={`status-badge status-badge--${tone}`}>
      {HEALTH_ICONS[tone === "pass" ? "healthy" : status] || ""} {label || HEALTH_LABELS[status] || status}
    </span>
  );
}

function AlertSeverityRow({ label, count, tone }) {
  return (
    <div className={`dashboard-alert-row tone-${tone}`}>
      <span>{label}</span>
      <strong>{count}</strong>
    </div>
  );
}

function UtilizationStatCard({ title, metric, statIcon, sparkData, sparkColor }) {
  if (!metric?.available) {
    return (
      <StatCard
        title={title}
        value="Metrics unavailable"
        detail={metric?.reason || "Metrics Server is not installed or accessible."}
        tone="default"
        unavailable
        icon={statIcon || <IconChartBar />}
      >
        {metric?.helpText ? (
          <p className="dashboard-metrics-help muted">{metric.helpText}</p>
        ) : null}
      </StatCard>
    );
  }

  const tone =
    utilizationTone(metric) === "fail"
      ? "danger"
      : utilizationTone(metric) === "warning"
        ? "warn"
        : "default";

  return (
    <StatCard
      title={title}
      value={formatUtilizationValue(metric)}
      tone={tone}
      icon={statIcon || <IconChartBar />}
    >
      {sparkData && sparkData.length > 1 ? (
        <Sparkline data={sparkData} color={sparkColor || "--accent"} />
      ) : null}
      <div className="dashboard-utilization-details">
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Used</span>
          <span className="upgrade-detail-value">{metric.usedDisplay}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Allocatable</span>
          <span className="upgrade-detail-value">{metric.allocatableDisplay}</span>
        </div>
      </div>
    </StatCard>
  );
}

function ActivityList({ items, emptyMessage }) {
  if (!items?.length) {
    return <p className="muted">{emptyMessage}</p>;
  }
  return (
    <ul className="dashboard-activity-list">
      {items.map((item, index) => (
        <li key={`${item.createdAt || item.time}-${index}`}>
          <span className="dashboard-activity-time">{item.time}</span>
          <span>{item.message}</span>
        </li>
      ))}
    </ul>
  );
}

export function ClusterHealthWidget({ summary }) {
  const healthStatus = summary?.health?.status || summary?.clusterHealth?.status || "unknown";
  const healthTone =
    healthStatus === "critical" ? "danger" : healthStatus === "warning" ? "warn" : "default";

  return (
    <StatCard
      title="Cluster Health"
      value={HEALTH_LABELS[healthStatus] || "Unknown"}
      detail={
        summary?.health?.reasons?.length
          ? summary.health.reasons.join(" · ")
          : "All core signals within normal range"
      }
      tone={healthTone}
      icon={<IconServer />}
    />
  );
}

export function KubernetesVersionWidget({ summary, canOpenUpgrade, onNavigateToUpgrade }) {
  const version = summary?.version || {};
  const clusterInfo = summary?.clusterInfo || {};
  const versionDisplay = versionStatusDisplay(version);
  const versionTone =
    versionStatusTone(version.status) === "pass"
      ? "default"
      : versionStatusTone(version.status) === "warning"
        ? "warn"
        : "default";

  const handleVersionClick = () => {
    if (canOpenUpgrade && onNavigateToUpgrade) {
      onNavigateToUpgrade();
    }
  };

  return (
    <StatCard
      title="Kubernetes Version"
      value={version.current || clusterInfo.version || "unknown"}
      detail={versionDisplay.message}
      tone={versionTone}
      icon={<IconHexagon />}
      onClick={canOpenUpgrade ? handleVersionClick : undefined}
      className={canOpenUpgrade ? "stat-card-link" : ""}
    >
      <div className="dashboard-version-mini">
        <span className="muted">
          Latest: {formatLatestVersion(version.latest || version.latestAvailable)}
        </span>
        {canOpenUpgrade ? (
          <span className="dashboard-version-link-hint">Open Upgrade Safe Mode →</span>
        ) : null}
      </div>
    </StatCard>
  );
}

export function CpuUsageWidget({ summary, series }) {
  return (
    <UtilizationStatCard
      title="CPU Usage"
      metric={summary?.cpuUsage}
      statIcon={<IconCpu />}
      sparkData={series?.cpu}
      sparkColor="--accent"
    />
  );
}

export function MemoryUsageWidget({ summary, series }) {
  return (
    <UtilizationStatCard
      title="Memory Usage"
      metric={summary?.memoryUsage}
      statIcon={<IconMemory />}
      sparkData={series?.mem}
      sparkColor="#8b7ff0"
    />
  );
}

export function NodesWidget({ summary }) {
  const nodes = summary?.nodes || { ready: 0, total: 0, status: "unknown" };
  return (
    <StatCard
      title="Nodes"
      value={`${nodes.ready} / ${nodes.total}`}
      detail={`Ready nodes · ${HEALTH_LABELS[nodes.status] || "Unknown"}`}
      tone={
        statusTone(nodes.status) === "fail"
          ? "danger"
          : statusTone(nodes.status) === "warning"
            ? "warn"
            : "default"
      }
      icon={<IconNodes />}
    />
  );
}

export function RunningPodsWidget({ summary }) {
  const pods = summary?.pods || { running: 0, pending: 0, failed: 0 };
  return (
    <StatCard
      title="Running Pods"
      value={pods.running}
      detail={`Pending: ${pods.pending} · Failed: ${pods.failed}`}
      tone={pods.failed > 0 ? "danger" : pods.pending > 0 ? "warn" : "default"}
      icon={<IconPods />}
    />
  );
}

export function InventorySummaryWidget({ summary, canOpenInventory, onNavigateToInventory }) {
  const inventory = summary?.inventory || {
    applications: 0,
    healthy: 0,
    warning: 0,
    critical: 0,
  };
  const tone =
    inventory.critical > 0 ? "danger" : inventory.warning > 0 ? "warn" : "default";

  return (
    <StatCard
      title="Applications"
      value={inventory.applications}
      detail={`Healthy: ${inventory.healthy} · Warning: ${inventory.warning} · Critical: ${inventory.critical}`}
      tone={tone}
      icon={<IconApps />}
      onClick={canOpenInventory && onNavigateToInventory ? onNavigateToInventory : undefined}
      className={canOpenInventory ? "stat-card-link" : ""}
    />
  );
}

export function ActiveAlertsWidget({ summary }) {
  const policyStats = summary?.alertPolicies || {};
  const alerts = summary?.alerts || { critical: 0, warning: 0, info: 0, total: 0 };
  const total = policyStats.activeTotal ?? alerts.total;
  return (
    <StatCard
      title="Active Alerts"
      value={total}
      detail={`Critical ${alerts.critical} · Warning ${alerts.warning} · Info ${alerts.info}`}
      tone={alerts.critical > 0 ? "danger" : alerts.warning > 0 ? "warn" : "default"}
      icon={<IconAlertCircle />}
    />
  );
}

export function CriticalAlertsWidget({ summary }) {
  const policyStats = summary?.alertPolicies || {};
  const alerts = summary?.alerts || {};
  const critical = policyStats.critical ?? alerts.critical ?? 0;
  return (
    <StatCard
      title="Critical Alerts"
      value={critical}
      detail={critical > 0 ? "Immediate attention required" : "No critical alerts active"}
      tone={critical > 0 ? "danger" : "default"}
      icon={<IconShieldExclaim />}
    />
  );
}

export function AlertsByClusterWidget({ summary }) {
  const clusters = summary?.alertPolicies?.byCluster || [];
  const clusterInfo = summary?.clusterInfo || {};
  return (
    <InfoCard title="Alerts by Cluster">
      {clusters.length === 0 ? (
        <p className="muted">No active policy alerts across clusters.</p>
      ) : (
        <ul className="dashboard-rank-list">
          {clusters.map((entry) => (
            <li key={entry.clusterId} className="dashboard-rank-item">
              <span>
                {entry.clusterId === clusterInfo.name || entry.clusterId === clusterInfo.contextName
                  ? clusterInfo.name || entry.clusterId
                  : entry.clusterId}
              </span>
              <strong>{entry.count}</strong>
            </li>
          ))}
        </ul>
      )}
    </InfoCard>
  );
}

export function AlertsBySeverityWidget({ summary }) {
  const bySeverity = summary?.alertPolicies?.bySeverity || summary?.alerts || {};
  const total = (bySeverity.critical || 0) + (bySeverity.warning || 0) + (bySeverity.info || 0);
  return (
    <InfoCard title="Alerts by Severity">
      {total === 0 ? (
        <p className="muted">No active policy alerts.</p>
      ) : (
        <div className="dashboard-alert-breakdown">
          <AlertSeverityRow label="Critical" count={bySeverity.critical || 0} tone="danger" />
          <AlertSeverityRow label="Warning" count={bySeverity.warning || 0} tone="warn" />
          <AlertSeverityRow label="Info" count={bySeverity.info || 0} tone="default" />
        </div>
      )}
    </InfoCard>
  );
}

export function TopTriggeredPoliciesWidget({ summary }) {
  const policies = summary?.alertPolicies?.topTriggeredPolicies || [];
  return (
    <InfoCard title="Top Triggered Policies">
      {policies.length === 0 ? (
        <p className="muted">No policies have fired recently.</p>
      ) : (
        <ul className="dashboard-rank-list">
          {policies.map((entry) => (
            <li key={entry.policyName} className="dashboard-rank-item">
              <span>{entry.policyName}</span>
              <strong>{entry.count}</strong>
            </li>
          ))}
        </ul>
      )}
    </InfoCard>
  );
}

export function VersionStatusWidget({ summary, canOpenUpgrade, onNavigateToUpgrade }) {
  const version = summary?.version || {};
  const clusterInfo = summary?.clusterInfo || {};
  const upgradeStatus = summary?.upgradeStatus || {};
  const versionDisplay = versionStatusDisplay(version);

  const handleVersionClick = () => {
    if (canOpenUpgrade && onNavigateToUpgrade) {
      onNavigateToUpgrade();
    }
  };

  return (
    <InfoCard title="Version Status">
      <div className="upgrade-details dashboard-details">
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Provider</span>
          <span className="upgrade-detail-value">{version.provider || clusterInfo.provider || "Unknown"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Current</span>
          <span className="upgrade-detail-value">{version.current || "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Latest</span>
          <span className="upgrade-detail-value">{formatLatestVersion(version.latest || version.latestAvailable)}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Status</span>
          <span className="upgrade-detail-value">
            <StatusBadge
              status={version.status === "up_to_date" ? "healthy" : version.status === "unknown" ? "unknown" : "warning"}
              label={versionDisplay.message}
            />
          </span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Upgrade Support</span>
          <span className="upgrade-detail-value">
            {version.upgradeSupportReason || upgradeStatus.reason || (version.upgradeSupported ? "Supported" : "Not supported")}
          </span>
        </div>
        {canOpenUpgrade ? (
          <button type="button" className="btn-outline dashboard-upgrade-link" onClick={handleVersionClick}>
            Open Upgrade Safe Mode
          </button>
        ) : null}
      </div>
    </InfoCard>
  );
}

export function ClusterInformationWidget({ summary }) {
  const clusterInfo = summary?.clusterInfo || {};
  return (
    <InfoCard title="Cluster Information">
      <div className="upgrade-details dashboard-details">
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Provider</span>
          <span className="upgrade-detail-value">{clusterInfo.provider || "Unknown"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Cluster Name</span>
          <span className="upgrade-detail-value">{clusterInfo.name || "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Current Context</span>
          <span className="upgrade-detail-value">{clusterInfo.contextName || "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Version</span>
          <span className="upgrade-detail-value">{clusterInfo.version || "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Nodes</span>
          <span className="upgrade-detail-value">{clusterInfo.nodeCount ?? "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Namespaces</span>
          <span className="upgrade-detail-value">{clusterInfo.namespaceCount ?? "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Pods</span>
          <span className="upgrade-detail-value">{clusterInfo.podCount ?? "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Last Sync</span>
          <span className="upgrade-detail-value">{formatDashboardTime(clusterInfo.lastSync)}</span>
        </div>
      </div>
    </InfoCard>
  );
}

export function AlertBreakdownWidget({ summary }) {
  const alerts = summary?.alerts || { critical: 0, warning: 0, info: 0, total: 0 };
  return (
    <InfoCard title="Alert Breakdown">
      {alerts.total === 0 ? (
        <p className="muted">No active alerts.</p>
      ) : (
        <div className="dashboard-alert-breakdown">
          <AlertSeverityRow label="Critical" count={alerts.critical} tone="danger" />
          <AlertSeverityRow label="Warning" count={alerts.warning} tone="warn" />
          <AlertSeverityRow label="Info" count={alerts.info} tone="default" />
        </div>
      )}
    </InfoCard>
  );
}

export function NamespaceHealthWidget({ summary }) {
  const namespaces = summary?.namespaces || [];
  return (
    <InfoCard title="Namespace Health">
      {namespaces.length ? (
        <ul className="dashboard-namespace-list">
          {namespaces.map((ns) => (
            <li key={ns.name} className={`dashboard-namespace-item status-${statusTone(ns.status)}`}>
              <div>
                <strong>{ns.name}</strong>
                <p className="muted">{ns.pods} pods</p>
              </div>
              <StatusBadge status={ns.status} label={HEALTH_LABELS[ns.status]} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No namespaces available for this cluster.</p>
      )}
    </InfoCard>
  );
}

function formatGiB(mib) {
  if (mib == null || Number.isNaN(Number(mib))) {
    return "—";
  }
  return `${(Number(mib) / 1024).toFixed(1)} GiB`;
}

function memoryBarTone(percent) {
  if (percent == null) return "default";
  if (percent >= 85) return "danger";
  if (percent >= 70) return "warn";
  return "ok";
}

export function NodeHealthWidget({ summary }) {
  const nodes = summary?.nodeHealth || [];
  return (
    <InfoCard title="Node Health">
      {nodes.length ? (
        <ul className="dashboard-node-list">
          {nodes.map((node) => {
            const pct = node.memoryPercent;
            const tone = memoryBarTone(pct);
            return (
              <li key={node.name} className={`dashboard-node-item status-${statusTone(node.status)}`}>
                <div className="dashboard-node-head">
                  <div className="dashboard-node-title">
                    <strong>{node.name}</strong>
                    {node.roles?.length ? (
                      <span className="dashboard-node-roles">{node.roles.join(", ")}</span>
                    ) : null}
                  </div>
                  <StatusBadge status={node.status} label={HEALTH_LABELS[node.status]} />
                </div>
                <div className="dashboard-node-metric">
                  <div className="dashboard-node-metric-label">
                    <span>Memory</span>
                    <span>
                      {formatGiB(node.memoryUsedMiB)} / {formatGiB(node.memoryTotalMiB)}
                      {pct != null ? ` · ${pct}%` : ""}
                    </span>
                  </div>
                  <div className="dashboard-node-bar">
                    <div
                      className={`dashboard-node-bar-fill tone-${tone}`}
                      style={{ width: `${Math.min(pct ?? 0, 100)}%` }}
                    />
                  </div>
                </div>
                <div className="dashboard-node-stats">
                  <span>{formatGiB(node.memoryAvailableMiB)} free</span>
                  <span>
                    CPU {node.cpuUsedCores ?? "—"} / {node.cpuTotalCores ?? "—"}
                    {node.cpuPercent != null ? ` (${node.cpuPercent}%)` : ""}
                  </span>
                  {node.kubeletVersion ? <span>{node.kubeletVersion}</span> : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted">No node metrics available for this cluster.</p>
      )}
    </InfoCard>
  );
}

export function MyAccessWidget({ summary }) {
  const access = summary?.myAccess || {};
  const clusters = access.clusters || [];
  const namespaces = access.namespaces || [];
  const resources = access.resources || [];
  const permissions = access.permissions || [];
  const counts = access.counts || {};

  const resourceItems = resources.map((item) => {
    if (typeof item === "string") {
      return item;
    }
    if (item?.label && item?.count != null) {
      return `${item.count} ${item.label}`;
    }
    return String(item);
  });

  return (
    <InfoCard title="My Access">
      <div className="dashboard-my-access">
        <div className="dashboard-my-access-section">
          <span className="upgrade-detail-label">Clusters</span>
          {clusters.length ? (
            <ul className="dashboard-my-access-list">
              {clusters.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
          ) : (
            <p className="muted dashboard-my-access-empty">None assigned</p>
          )}
        </div>
        <div className="dashboard-my-access-section">
          <span className="upgrade-detail-label">Namespaces</span>
          {namespaces.length ? (
            <ul className="dashboard-my-access-list">
              {namespaces.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
          ) : (
            <p className="muted dashboard-my-access-empty">—</p>
          )}
        </div>
        <div className="dashboard-my-access-section">
          <span className="upgrade-detail-label">Resources</span>
          {resourceItems.length ? (
            <ul className="dashboard-my-access-list">
              {resourceItems.map((label) => (
                <li key={label}>{label}</li>
              ))}
            </ul>
          ) : counts.total > 0 ? (
            <p className="dashboard-my-access-empty">
              Pods: {counts.pods}, Deployments: {counts.deployments}, Services: {counts.services}
            </p>
          ) : (
            <p className="muted dashboard-my-access-empty">—</p>
          )}
        </div>
        <div className="dashboard-my-access-section">
          <span className="upgrade-detail-label">Allowed Actions</span>
          {permissions.length ? (
            <ul className="dashboard-my-access-permissions">
              {permissions.map((perm) => (
                <li key={perm.id} style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                  <svg viewBox="0 0 16 16" fill="currentColor" width="12" height="12" aria-hidden="true" style={{ color: "var(--ok)", flexShrink: 0 }}><path fillRule="evenodd" d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" clipRule="evenodd" /></svg>
                  {perm.label}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted dashboard-my-access-empty">—</p>
          )}
        </div>
      </div>
    </InfoCard>
  );
}

export function RecentActivityWidget({ summary }) {
  return (
    <InfoCard title="Recent Activity">
      <ActivityList items={summary?.recentActivity} emptyMessage="No recent activity available." />
    </InfoCard>
  );
}

export function OperationalEventsWidget({ summary }) {
  return (
    <InfoCard title="Operational Events">
      <ActivityList items={summary?.operationalEvents} emptyMessage="No operational events recorded." />
    </InfoCard>
  );
}

export function UserActivityWidget({ summary }) {
  return (
    <InfoCard title="User Activity">
      <ActivityList items={summary?.userActivity} emptyMessage="No user activity recorded." />
    </InfoCard>
  );
}

export function UpgradeStatusWidget({ summary }) {
  const upgradeStatus = summary?.upgradeStatus || {};
  const version = summary?.version || {};
  const versionDisplay = versionStatusDisplay(version);

  const precheckLabel =
    upgradeStatus.precheckStatus === "passed"
      ? "Passed"
      : upgradeStatus.precheckStatus === "failed"
        ? "Failed"
        : upgradeStatus.precheckStatus === "none"
          ? "Not run"
          : "Unknown";

  return (
    <InfoCard title="Upgrade Status">
      <div className="upgrade-details dashboard-details">
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Current</span>
          <span className="upgrade-detail-value">{upgradeStatus.currentVersion || version.current || "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Latest</span>
          <span className="upgrade-detail-value">{formatLatestVersion(upgradeStatus.latestAvailable || version.latest)}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Provider</span>
          <span className="upgrade-detail-value">{upgradeStatus.provider || version.provider || "—"}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Version Status</span>
          <span className="upgrade-detail-value">{versionDisplay.message}</span>
        </div>
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Upgrade Supported</span>
          <span className="upgrade-detail-value">
            <StatusBadge
              status={upgradeStatus.upgradeSupported || version.upgradeSupported ? "healthy" : "warning"}
              label={upgradeStatus.upgradeSupported || version.upgradeSupported ? "Yes" : "No"}
            />
          </span>
        </div>
        {upgradeStatus.reason || version.upgradeSupportReason ? (
          <div className="upgrade-detail-row">
            <span className="upgrade-detail-label">Reason</span>
            <span className="upgrade-detail-value">{upgradeStatus.reason || version.upgradeSupportReason}</span>
          </div>
        ) : null}
        <div className="upgrade-detail-row">
          <span className="upgrade-detail-label">Precheck</span>
          <span className="upgrade-detail-value">
            <StatusBadge
              status={
                upgradeStatus.precheckStatus === "passed"
                  ? "healthy"
                  : upgradeStatus.precheckStatus === "failed"
                    ? "critical"
                    : "unknown"
              }
              label={precheckLabel}
            />
          </span>
        </div>
      </div>
    </InfoCard>
  );
}
