import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import TriageTiles from "../components/alerts/TriageTiles.jsx";
import ActivityStrip from "../components/alerts/ActivityStrip.jsx";
import AlertFeed from "../components/alerts/AlertFeed.jsx";
import AlertDetailDrawer from "../components/alerts/AlertDetailDrawer.jsx";
import AlertHistoryTab from "../components/alerts/AlertHistoryTab.jsx";
import { listAlertHistory } from "../api/alertPoliciesApi.js";
import { isNamespaceScopeLoading, SCOPE_LOADING_HINT } from "../utils/accessViewState.js";
import {
  buildAlertsScopeSummary,
  consumeAlertsTabHint,
  hasAlertMonitoringScope,
} from "../lib/alertDisplay.js";
import {
  bucketAlertHistory,
  formatDurationShort,
  groupAlerts,
  resolvedStats,
  severitySeries,
} from "../lib/alertFeed.js";
import { EMPTY_MESSAGES, isAccessDeniedError } from "../utils/authz.js";

const AlertPoliciesPage = lazy(() => import("./AlertPoliciesPage.jsx"));
const AlertRoutingPage = lazy(() => import("./AlertRoutingPage.jsx"));

const HISTORY_LIMIT = 300;
const HISTORY_REFRESH_MS = 60000;

const TYPE_FILTERS = [
  { key: "all", label: "All" },
  { key: "metric", label: "Metric" },
  { key: "log", label: "Log" },
  { key: "service", label: "Service" },
  { key: "automation", label: "Automation" },
];

function IconSearch(props) {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}>
      <circle cx="11" cy="11" r="7" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}

function alertMatchesQuery(alert, query) {
  const haystack = [
    alert.title,
    alert.policyName,
    alert.namespace,
    alert.pod,
    alert.resourceName,
    alert.serviceName,
    alert.ticketNumber,
    alert.matchedPattern,
    alert.description,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function ScopeChips({ scope, resourceCount }) {
  if (!scope.clusterId) {
    return null;
  }
  return (
    <div className="al-scope">
      <span className="al-scope-chip">
        <span className="al-scope-k">cluster</span>
        <span className="al-mono">{scope.clusterLabel}</span>
      </span>
      {scope.namespaces.length ? (
        <span className="al-scope-chip" title={scope.namespaces.join(", ")}>
          <span className="al-scope-k">namespaces</span>
          <span className="al-mono">
            {scope.namespaces.length <= 3
              ? scope.namespaces.join(" · ")
              : `${scope.namespaces.slice(0, 2).join(" · ")} +${scope.namespaces.length - 2}`}
          </span>
        </span>
      ) : (
        <span className="al-scope-chip">
          <span className="al-scope-k">namespaces</span>all accessible
        </span>
      )}
      {resourceCount ? (
        <span className="al-scope-chip" title={`${resourceCount} assigned resources in scope`}>
          <span className="al-scope-k">resources</span>
          <span className="al-mono">{resourceCount}</span>
        </span>
      ) : null}
    </div>
  );
}

function AllClear({ clusterLabel, lastResolved, nowTs, onOpenHistory }) {
  return (
    <section className="card al-clear">
      <div className="al-ring" aria-hidden="true">
        <svg width="88" height="88" viewBox="0 0 88 88">
          <circle cx="44" cy="44" r="38" fill="none" className="al-ring-track" strokeWidth="8" />
          <circle cx="44" cy="44" r="38" fill="none" className="al-ring-arc" strokeWidth="8" strokeLinecap="round" transform="rotate(-90 44 44)" />
        </svg>
        <span className="al-ring-check">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12" /></svg>
        </span>
      </div>
      <h4>All clear in {clusterLabel}</h4>
      {lastResolved ? (
        <p>
          Everything in your scope is healthy. Last alert resolved{" "}
          <b>{formatDurationShort(nowTs - lastResolved.resolvedTs) || "just now"} ago</b> —{" "}
          <span className="al-mono">
            {lastResolved.title}
            {lastResolved.resourceName ? ` · ${lastResolved.resourceName}` : ""}
          </span>
          {lastResolved.durationMs != null ? `, after ${formatDurationShort(lastResolved.durationMs)}` : ""}.
        </p>
      ) : (
        <p>Everything you have access to is currently operating normally.</p>
      )}
      <button type="button" className="al-ghostlink" onClick={onOpenHistory}>
        Review the last 24 hours →
      </button>
    </section>
  );
}

/**
 * The Alerts section: Open (triage feed) · History · Policies · Routing.
 * Consolidates the former Alerts / Alert Policies / Alert Routing pages under
 * one address with the same RBAC gates each page had.
 */
export default function AlertsPage({
  data,
  selectedClusterId,
  allowedClusters,
  allowedNamespaces,
  allowedResources,
  selectedNamespace = "",
  canManageAlerts = false,
  canViewRouting = false,
  hasClusters,
  authUser,
  coreLoading = false,
  namespacesLoading = false,
  accessError = "",
}) {
  const alerts = data.alerts || [];
  const [tab, setTab] = useState(() => {
    const hint = consumeAlertsTabHint();
    if (hint === "history" || hint === "policies") {
      return hint;
    }
    if (hint === "routing" && canViewRouting) {
      return hint;
    }
    return "open";
  });
  const [severityFilter, setSeverityFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [collapsedGroups, setCollapsedGroups] = useState(() => new Set());
  const [drawerAlert, setDrawerAlert] = useState(null);
  const [history, setHistory] = useState({ items: [], loading: false, error: "", loaded: false });

  const nowTs = Date.now();

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

  /* ── history feed (activity strip, sparklines, resolved stats, History tab) ── */
  const fetchHistory = useCallback(async () => {
    if (!selectedClusterId || !hasClusters) {
      return;
    }
    setHistory((prev) => ({ ...prev, loading: !prev.loaded }));
    try {
      const response = await listAlertHistory({ cluster: selectedClusterId, limit: HISTORY_LIMIT });
      setHistory({ items: response.items || [], loading: false, error: "", loaded: true });
    } catch (historyError) {
      setHistory((prev) => ({
        items: prev.items,
        loading: false,
        error: historyError.message || "Failed to load alert history.",
        loaded: true,
      }));
    }
  }, [selectedClusterId, hasClusters]);

  useEffect(() => {
    setHistory({ items: [], loading: false, error: "", loaded: false });
    if (!selectedClusterId || !hasClusters || accessError) {
      return undefined;
    }
    fetchHistory();
    const timer = window.setInterval(fetchHistory, HISTORY_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [fetchHistory, selectedClusterId, hasClusters, accessError]);

  /* ── derived feed data ── */
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

  const filteredAlerts = useMemo(() => {
    const trimmedQuery = query.trim().toLowerCase();
    return alerts.filter((alert) => {
      if (severityFilter) {
        const value = String(alert.severity || "").toLowerCase();
        const normalized = value === "critical" || value === "warning" ? value : "info";
        if (normalized !== severityFilter) {
          return false;
        }
      }
      if (typeFilter !== "all" && (alert.alertType || "metric") !== typeFilter) {
        return false;
      }
      if (trimmedQuery && !alertMatchesQuery(alert, trimmedQuery)) {
        return false;
      }
      return true;
    });
  }, [alerts, severityFilter, typeFilter, query]);

  const feedEntries = useMemo(() => groupAlerts(filteredAlerts), [filteredAlerts]);

  /* nowTs is intentionally excluded from deps: history refetches every 60s,
     so buckets realign at worst one minute late instead of on every render. */
  const activity = useMemo(
    () => bucketAlertHistory(history.items, { nowTs }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [history.items]
  );

  const resolvedInfo = useMemo(
    () => (history.loaded && !history.error ? resolvedStats(history.items, { nowTs }) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [history.items, history.loaded, history.error]
  );

  const sparks = useMemo(() => {
    if (!history.loaded || history.error) {
      return null;
    }
    const resolvedActivity = bucketAlertHistory(
      history.items
        .filter((row) => String(row.status || "").toLowerCase() === "resolved" && row.resolvedAt)
        .map((row) => ({ severity: row.severity, firedAt: row.resolvedAt })),
      { nowTs }
    );
    return {
      critical: severitySeries(activity.buckets, "critical"),
      warning: severitySeries(activity.buckets, "warning"),
      info: severitySeries(activity.buckets, "info"),
      resolved: resolvedActivity.buckets.map((bucket) => bucket.total),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activity, history.items, history.loaded, history.error]);

  const toggleGroup = (key) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const openHistoryTab = () => setTab("history");

  const handleViewPolicy = () => {
    setDrawerAlert(null);
    setTab("policies");
  };

  /* ── scope gates (Open + History tabs share these) ── */
  const scopeLoading = isNamespaceScopeLoading({ coreLoading, namespacesLoading });

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

  const hasFilters = Boolean(severityFilter || typeFilter !== "all" || query.trim());
  const clearFilters = () => {
    setSeverityFilter("");
    setTypeFilter("all");
    setQuery("");
  };

  const tabs = [
    { key: "open", label: "Open", count: alerts.length },
    { key: "history", label: "History" },
    { key: "policies", label: "Policies" },
    ...(canViewRouting ? [{ key: "routing", label: "Routing" }] : []),
  ];

  return (
    <div className="ops-page alerts-page">
      <header className="sg-ph al-ph">
        <div className="al-ph-main">
          <h2>Alerts</h2>
          <ScopeChips scope={scope} resourceCount={scope.resources.length} />
        </div>
        <div className="al-tabs" role="tablist" aria-label="Alerts section">
          {tabs.map((entry) => (
            <button
              key={entry.key}
              type="button"
              role="tab"
              aria-selected={tab === entry.key}
              onClick={() => setTab(entry.key)}
            >
              {entry.label}
              {entry.key === "open" ? (
                <span className="al-tab-n">{entry.count}</span>
              ) : null}
            </button>
          ))}
        </div>
      </header>

      {tab === "open" ? (
        gateContent || (
          <>
            <TriageTiles
              counts={severityCounts}
              sparks={sparks}
              resolved={resolvedInfo}
              activeSeverity={severityFilter}
              onToggleSeverity={(severity) =>
                setSeverityFilter((prev) => (prev === severity ? "" : severity))
              }
              onOpenHistory={openHistoryTab}
            />

            {!history.error ? (
              <ActivityStrip
                buckets={activity.buckets}
                maxTotal={activity.maxTotal}
                total={activity.total}
                loading={!history.loaded && history.loading}
              />
            ) : null}

            {alerts.length ? (
              <>
                <div className="al-toolbar">
                  <div className="al-search">
                    <IconSearch />
                    <input
                      type="search"
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Filter by resource, policy, pattern…"
                      aria-label="Filter alerts"
                    />
                  </div>
                  <div className="al-seg" role="group" aria-label="Alert type">
                    {TYPE_FILTERS.map((entry) => (
                      <button
                        key={entry.key}
                        type="button"
                        aria-pressed={typeFilter === entry.key}
                        onClick={() => setTypeFilter(entry.key)}
                      >
                        {entry.label}
                      </button>
                    ))}
                  </div>
                  <span className="al-count">
                    {hasFilters
                      ? `${filteredAlerts.length} of ${alerts.length} shown`
                      : `${alerts.length} open · ${feedEntries.length} item${feedEntries.length === 1 ? "" : "s"} after grouping`}
                  </span>
                </div>

                {feedEntries.length ? (
                  <AlertFeed
                    entries={feedEntries}
                    nowTs={nowTs}
                    collapsedGroups={collapsedGroups}
                    onToggleGroup={toggleGroup}
                    onOpenAlert={setDrawerAlert}
                  />
                ) : (
                  <section className="card al-filter-empty">
                    <p className="muted">No alerts match the current filters.</p>
                    <button type="button" className="al-ghostlink" onClick={clearFilters}>
                      Clear filters
                    </button>
                  </section>
                )}
              </>
            ) : (
              <AllClear
                clusterLabel={scope.clusterLabel}
                lastResolved={resolvedInfo?.lastResolved || null}
                nowTs={nowTs}
                onOpenHistory={openHistoryTab}
              />
            )}
          </>
        )
      ) : null}

      {tab === "history" ? (
        gateContent || (
          <AlertHistoryTab
            items={history.items}
            loading={history.loading}
            error={history.error}
            nowTs={nowTs}
            onOpenAlert={setDrawerAlert}
          />
        )
      ) : null}

      {tab === "policies" ? (
        <Suspense fallback={<LoadingState label="Loading alert policies..." />}>
          <AlertPoliciesPage
            embedded
            clusterId={selectedClusterId}
            clusterOptions={allowedClusters}
            selectedNamespace={selectedNamespace}
            allowedNamespaces={allowedNamespaces}
            hasClusters={hasClusters}
            canManage={canManageAlerts}
            coreLoading={coreLoading}
            accessError={accessError}
          />
        </Suspense>
      ) : null}

      {tab === "routing" && canViewRouting ? (
        <Suspense fallback={<LoadingState label="Loading alert routing..." />}>
          <AlertRoutingPage embedded />
        </Suspense>
      ) : null}

      <AlertDetailDrawer
        alert={drawerAlert}
        clusterLabel={scope.clusterLabel}
        onClose={() => setDrawerAlert(null)}
        onViewPolicy={handleViewPolicy}
      />
    </div>
  );
}
