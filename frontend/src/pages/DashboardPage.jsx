import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { EMPTY_MESSAGES, isAccessDeniedError } from "../utils/authz.js";
import { useAuth } from "../context/AuthContext.jsx";
import { formatDashboardTime } from "../utils/dashboardStatus.js";
import { getDashboardWidgetRegistry, sortWidgetsForUser } from "../dashboard/widgetRegistry.js";
import { getVisibleWidgets, groupWidgetsBySection } from "../dashboard/widgetVisibility.js";

export default function DashboardPage({
  summary,
  loading,
  coreLoading = false,
  accessError = "",
  hasClusters = true,
  selectedCluster,
  onRefresh,
  lastRefreshedAt,
  onNavigateToUpgrade,
  onNavigateToInventory,
  canOpenUpgrade,
  canOpenInventory,
}) {
  const auth = useAuth();
  const clusterId = selectedCluster?.id;
  const isAdmin = auth.isAdmin;

  const widgetRegistry = getDashboardWidgetRegistry(isAdmin);
  const visibleWidgets = sortWidgetsForUser(
    getVisibleWidgets(widgetRegistry, auth, { clusterId }),
    isAdmin
  );
  const myAccessWidget = !isAdmin ? visibleWidgets.find((widget) => widget.id === "myAccess") : null;
  const layoutWidgets = myAccessWidget
    ? visibleWidgets.filter((widget) => widget.id !== "myAccess")
    : visibleWidgets;
  const sections = groupWidgetsBySection(layoutWidgets);

  const pageTitle = isAdmin ? "Operations Dashboard" : "Dashboard";
  const pageSubtitle = isAdmin
    ? "Live cluster health and operational signals."
    : "Your assigned clusters, workloads, and alerts.";

  const widgetProps = {
    summary,
    selectedCluster,
    canOpenUpgrade,
    onNavigateToUpgrade,
    onNavigateToInventory,
    canOpenInventory,
  };

  const hasNoAccessibleScope =
    summary &&
    !auth.isAdmin &&
    summary.myAccess?.hasAccessibleScope === false &&
    !auth.canAccessCluster(clusterId) &&
    !auth.hasAnyClusterAccess();

  const hasVisibleContent =
    Boolean(myAccessWidget) ||
    sections.stats?.length ||
    sections.details?.length ||
    sections.activity?.length ||
    sections.full?.length;

  if (coreLoading || (loading && !summary)) {
    return (
      <>
        <PageTitle title={pageTitle} subtitle={pageSubtitle} />
        <p className="muted dashboard-loading">
          {coreLoading ? "Loading clusters..." : "Loading dashboard data..."}
        </p>
      </>
    );
  }

  if (isAccessDeniedError(accessError)) {
    return (
      <>
        <PageTitle title={pageTitle} subtitle={pageSubtitle} />
        <AccessDeniedPage message={accessError} />
      </>
    );
  }

  if (accessError) {
    return (
      <>
        <PageTitle title={pageTitle} subtitle={pageSubtitle} />
        <ErrorBanner message={accessError} suppressAccessDenied={false} />
      </>
    );
  }

  if (!hasClusters) {
    return (
      <>
        <PageTitle title={pageTitle} subtitle={pageSubtitle} />
        <EmptyState message={EMPTY_MESSAGES.noClusters} />
      </>
    );
  }

  if (!selectedCluster) {
    return (
      <>
        <PageTitle title={pageTitle} subtitle={pageSubtitle} />
        <section className="card dashboard-empty">
          <p className="muted">No cluster selected.</p>
        </section>
      </>
    );
  }

  if (hasNoAccessibleScope) {
    return (
      <>
        <PageTitle title={pageTitle} subtitle={pageSubtitle} />
        <EmptyState
          message={EMPTY_MESSAGES.noResources}
          hint="Contact an administrator."
        />
      </>
    );
  }

  const clusterInfo = summary?.clusterInfo || {};
  const MyAccessPanel = myAccessWidget?.component;

  return (
    <>
      <PageTitle
        title={pageTitle}
        subtitle={
          isAdmin
            ? `${clusterInfo.name || selectedCluster.name} — live operational view`
            : `${clusterInfo.name || selectedCluster.name} — your operational workspace`
        }
        actionLabel="Refresh"
        onAction={onRefresh}
      />

      <div className="dashboard-meta">
        <span className="muted">
          Last Updated: {formatDashboardTime(lastRefreshedAt || summary?.lastUpdated)}
        </span>
      </div>

      {!hasVisibleContent ? (
        <EmptyState
          message="No dashboard panels are available for your role and granted actions."
          hint="Ask an administrator to grant cluster access and overview permissions."
        />
      ) : null}

      {MyAccessPanel ? (
        <section className="dashboard-row dashboard-row-single dashboard-my-access-first">
          <MyAccessPanel key={myAccessWidget.id} {...widgetProps} />
        </section>
      ) : null}

      {sections.stats?.length ? (
        <section className="stat-grid dashboard-stat-grid">
          {sections.stats.map((widget) => {
            const Widget = widget.component;
            return <Widget key={widget.id} {...widgetProps} />;
          })}
        </section>
      ) : null}

      {sections.details?.length ? (
        <section className="content-grid dashboard-row">
          {sections.details.map((widget) => {
            const Widget = widget.component;
            return <Widget key={widget.id} {...widgetProps} />;
          })}
        </section>
      ) : null}

      {sections.activity?.length ? (
        <section className="content-grid dashboard-row">
          {sections.activity.map((widget) => {
            const Widget = widget.component;
            return <Widget key={widget.id} {...widgetProps} />;
          })}
        </section>
      ) : null}

      {sections.full?.length ? (
        <section className="content-grid dashboard-row dashboard-row-single">
          {sections.full.map((widget) => {
            const Widget = widget.component;
            return <Widget key={widget.id} {...widgetProps} />;
          })}
        </section>
      ) : null}
    </>
  );
}
