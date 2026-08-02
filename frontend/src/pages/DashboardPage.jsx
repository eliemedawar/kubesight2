import { useMemo, useState } from "react";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import { EMPTY_MESSAGES, isAccessDeniedError } from "../utils/authz.js";
import { useAuth } from "../context/AuthContext.jsx";
import { getDashboardWidgetRegistry, sortWidgetsForUser } from "../dashboard/widgetRegistry.js";
import { getVisibleWidgets, groupWidgetsBySection } from "../dashboard/widgetVisibility.js";
import { useDashboardSeries } from "../dashboard/useDashboardSeries.js";
import { useDashboardSummary } from "../dashboard/useDashboardSummary.js";
import { buildAttentionFeed } from "../dashboard/attentionFeed.js";
import AttentionFeed from "../dashboard/AttentionFeed.jsx";
import OpsDashboard from "../dashboard/OpsDashboard.jsx";
import DashboardSkeleton from "../dashboard/DashboardSkeleton.jsx";

export default function DashboardPage({
  coreLoading = false,
  accessError: shellError = "",
  hasClusters = true,
  selectedCluster,
  refreshIntervalSeconds,
  onClusterMissing,
  onNavigateToUpgrade,
  onNavigateToInventory,
  canOpenUpgrade,
  canOpenInventory,
  alerts,
  integrations,
  approvals,
  unavailableSources,
}) {
  const auth = useAuth();
  const [timeRange, setTimeRange] = useState("6h");
  const clusterId = selectedCluster?.id;
  const isAdmin = auth.isAdmin;

  // This page owns its own fetch and poller now. Leaving the dashboard unmounts
  // the component and stops the interval, so there is no active-page check left
  // to get wrong.
  const {
    summary,
    refreshedAt,
    loading,
    refreshing,
    error: summaryError,
    refresh,
  } = useDashboardSummary({
    clusterId,
    enabled: Boolean(clusterId),
    refreshIntervalSeconds,
    canAccessCluster: auth.canAccessCluster,
    onClusterMissing,
  });

  // The shell still reports failures that are not this page's own — losing the
  // cluster list, for instance.
  const accessError = summaryError || shellError;
  const summaryReady = Boolean(summary && clusterId && summary.clusterId === clusterId);
  const series = useDashboardSeries(summaryReady ? summary : null, timeRange);

  // "What needs attention?" in priority order, composed from what the app
  // already holds. Sources that failed to load are named rather than silently
  // shortening the list — see attentionFeed.js.
  const feed = useMemo(
    () =>
      buildAttentionFeed({
        alerts,
        integrations,
        approvals,
        summary: summaryReady ? summary : null,
        clusterId,
        unavailable: unavailableSources,
        limit: 8,
      }),
    [alerts, integrations, approvals, summary, summaryReady, clusterId, unavailableSources]
  );

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
    series,
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
      {/*
        First, above everything. The dashboard reported state without ranking
        it, so an operator opening it at the start of a shift had to assemble
        the priority list themselves from several panels. The charts are still
        below; they answer a different question.
      */}
      <section className="dashboard-row dashboard-row-single">
        <AttentionFeed feed={feed} loading={loading || coreLoading} />
      </section>

      {MyAccessPanel ? (
        <section className="dashboard-row dashboard-row-single dashboard-my-access-first">
          <MyAccessPanel key={myAccessWidget.id} {...widgetProps} />
        </section>
      ) : null}

      <OpsDashboard
        summary={summary}
        series={series}
        timeRange={timeRange}
        onTimeRangeChange={setTimeRange}
        lastRefreshedAt={refreshedAt}
        onRefresh={refresh}
        canOpenUpgrade={canOpenUpgrade}
        onNavigateToUpgrade={onNavigateToUpgrade}
      />
    </>
  );
}
