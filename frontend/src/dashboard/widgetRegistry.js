import {
  ActiveAlertsWidget,
  AlertBreakdownWidget,
  AlertsByClusterWidget,
  AlertsBySeverityWidget,
  ClusterHealthWidget,
  CriticalAlertsWidget,
  InventorySummaryWidget,
  ClusterInformationWidget,
  CpuUsageWidget,
  KubernetesVersionWidget,
  MemoryUsageWidget,
  MyAccessWidget,
  NamespaceHealthWidget,
  NodesWidget,
  OperationalEventsWidget,
  RecentActivityWidget,
  RunningPodsWidget,
  TopTriggeredPoliciesWidget,
  UpgradeStatusWidget,
  UserActivityWidget,
  VersionStatusWidget,
} from "./widgets/DashboardWidgets.jsx";

/**
 * Dashboard widget registry.
 * Add widgets by declaring permissions, access scope, and component.
 */
export const DASHBOARD_WIDGETS = [
  {
    id: "clusterHealth",
    section: "stats",
    title: "Cluster Health",
    requiredPermissions: ["clusters:view"],
    requiresClusterAccess: true,
    component: ClusterHealthWidget,
  },
  {
    id: "kubernetesVersion",
    section: "stats",
    title: "Kubernetes Version",
    requiredPermissions: ["overview:view"],
    requiresClusterAccess: true,
    component: KubernetesVersionWidget,
  },
  {
    id: "cpuUsage",
    section: "stats",
    title: "CPU Usage",
    requiredPermissions: ["overview:view"],
    requiresClusterAccess: true,
    component: CpuUsageWidget,
  },
  {
    id: "memoryUsage",
    section: "stats",
    title: "Memory Usage",
    requiredPermissions: ["overview:view"],
    requiresClusterAccess: true,
    component: MemoryUsageWidget,
  },
  {
    id: "nodes",
    section: "stats",
    title: "Nodes",
    requiredPermissions: ["overview:view"],
    requiresClusterAccess: true,
    component: NodesWidget,
  },
  {
    id: "runningPods",
    section: "stats",
    title: "Running Pods",
    requiredAnyPermissions: ["pods:view", "resources:view"],
    requiresClusterAccess: true,
    component: RunningPodsWidget,
  },
  {
    id: "inventorySummary",
    section: "stats",
    title: "Applications",
    requiredAnyPermissions: ["resources:view", "deployments:view"],
    requiresClusterAccess: true,
    component: InventorySummaryWidget,
  },
  {
    id: "activeAlerts",
    section: "stats",
    title: "Active Alerts",
    requiredPermissions: ["alerts:view"],
    requiresClusterAccess: true,
    component: ActiveAlertsWidget,
  },
  {
    id: "criticalAlerts",
    section: "stats",
    title: "Critical Alerts",
    requiredPermissions: ["alerts:view"],
    requiresClusterAccess: true,
    component: CriticalAlertsWidget,
  },
  {
    id: "versionStatus",
    section: "details",
    title: "Version Status",
    requiredPermissions: ["overview:view"],
    requiresClusterAccess: true,
    component: VersionStatusWidget,
  },
  {
    id: "clusterInformation",
    section: "details",
    title: "Cluster Information",
    requiredPermissions: ["overview:view", "clusters:view"],
    requiresClusterAccess: true,
    component: ClusterInformationWidget,
  },
  {
    id: "alertBreakdown",
    section: "details",
    title: "Alert Breakdown",
    requiredPermissions: ["alerts:view"],
    requiresClusterAccess: true,
    component: AlertBreakdownWidget,
  },
  {
    id: "alertsByCluster",
    section: "details",
    title: "Alerts by Cluster",
    requiredPermissions: ["alerts:view"],
    requiresClusterAccess: true,
    component: AlertsByClusterWidget,
  },
  {
    id: "alertsBySeverity",
    section: "details",
    title: "Alerts by Severity",
    requiredPermissions: ["alerts:view"],
    requiresClusterAccess: true,
    component: AlertsBySeverityWidget,
  },
  {
    id: "topTriggeredPolicies",
    section: "details",
    title: "Top Triggered Policies",
    requiredPermissions: ["alerts:view"],
    requiresClusterAccess: true,
    component: TopTriggeredPoliciesWidget,
  },
  {
    id: "namespaceHealth",
    section: "details",
    title: "Namespace Health",
    requiredPermissions: ["namespaces:view"],
    requiresClusterAccess: true,
    component: NamespaceHealthWidget,
  },
  {
    id: "myAccess",
    section: "details",
    title: "My Access",
    alwaysVisible: true,
    component: MyAccessWidget,
  },
  {
    id: "recentActivity",
    section: "activity",
    title: "Recent Activity",
    requiredPermissions: ["audit:view"],
    requiresClusterAccess: true,
    component: RecentActivityWidget,
  },
  {
    id: "operationalEvents",
    section: "activity",
    title: "Operational Events",
    requiredPermissions: ["overview:view"],
    hideIfPermissions: ["audit:view"],
    requiresClusterAccess: true,
    component: OperationalEventsWidget,
  },
  {
    id: "userActivity",
    section: "activity",
    title: "User Activity",
    requiredPermissions: ["users:view"],
    component: UserActivityWidget,
  },
  {
    id: "upgradeStatus",
    section: "full",
    title: "Upgrade Status",
    requiredPermissions: ["upgrades:precheck"],
    requiresClusterAccess: true,
    component: UpgradeStatusWidget,
  },
];

/** Widgets shown on Dashboard Home for non-admin users (operational read-only view). */
export const USER_DASHBOARD_WIDGET_IDS = [
  "myAccess",
  "clusterHealth",
  "runningPods",
  "activeAlerts",
  "criticalAlerts",
  "cpuUsage",
  "memoryUsage",
  "kubernetesVersion",
  "inventorySummary",
  "namespaceHealth",
  "alertBreakdown",
  "alertsByCluster",
  "alertsBySeverity",
  "topTriggeredPolicies",
  "versionStatus",
  "operationalEvents",
];

const USER_WIDGET_ORDER = Object.fromEntries(
  USER_DASHBOARD_WIDGET_IDS.map((id, index) => [id, index])
);

export function getDashboardWidgetRegistry(isAdmin) {
  if (isAdmin) {
    return DASHBOARD_WIDGETS;
  }
  return DASHBOARD_WIDGETS.filter((widget) => USER_DASHBOARD_WIDGET_IDS.includes(widget.id));
}

export function sortWidgetsForUser(widgets, isAdmin) {
  if (isAdmin) {
    return widgets;
  }
  return [...widgets].sort(
    (a, b) => (USER_WIDGET_ORDER[a.id] ?? 99) - (USER_WIDGET_ORDER[b.id] ?? 99)
  );
}
