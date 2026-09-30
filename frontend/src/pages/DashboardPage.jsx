import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { EMPTY_MESSAGES, isAccessDeniedError } from "../utils/authz.js";
import { useAuth } from "../context/AuthContext.jsx";
import { getDashboardWidgetRegistry, sortWidgetsForUser } from "../dashboard/widgetRegistry.js";
import { getVisibleWidgets, groupWidgetsBySection } from "../dashboard/widgetVisibility.js";
import OpsDashboard from "../dashboard/OpsDashboard.jsx";
import DashboardSkeleton from "../dashboard/DashboardSkeleton.jsx";

export default function DashboardPage({
  summary,
  loading,
  refreshing = false,
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
  onNavigate,
  canOpenPage,
}) {
  const auth = useAuth();
  const clusterId = selectedCluster?.id;
  const summaryReady = Boolean(summary && clusterId && summary.clusterId === clusterId);
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

  if (coreLoading) {
    return <DashboardSkeleton />;
  }

  if (!summaryReady) {
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
    // Paint the dashboard structure immediately (chart shells + skeleton rows)
    // instead of a blank "Loading…" screen; it hydrates when the summary lands.
    return <DashboardSkeleton />;
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

  const MyAccessPanel = myAccessWidget?.component;

  return (
    <>
      {MyAccessPanel ? (
        <section className="dashboard-row dashboard-row-single dashboard-my-access-first">
          <MyAccessPanel key={myAccessWidget.id} {...widgetProps} />
        </section>
      ) : null}

      <OpsDashboard
        summary={summary}
        isAdmin={isAdmin}
        lastRefreshedAt={lastRefreshedAt}
        refreshing={refreshing}
        onRefresh={onRefresh}
        onNavigate={onNavigate}
        canOpen={canOpenPage}
      />
    </>
  );
}
