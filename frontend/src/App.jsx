import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  getClusterOverview,
  getSettings,
  listAlerts,
  listClusters,
  testAlertEmail,
  updateSettings,
} from "./api";
import { useAuth } from "./context/AuthContext";
import { useChangeBundle } from "./context/ChangeBundleContext";
import ChangeBundleDrawer from "./components/changes/ChangeBundleDrawer.jsx";
import AppShell from "./components/layout/AppShell.jsx";
import RouteLoadingFallback from "./components/common/RouteLoadingFallback.jsx";
import { emptyNamespaceResources } from "./lib/resourceTypes.js";
import {
  EMPTY_MESSAGES,
  formatAccessError,
} from "./utils/authz.js";
import {
  getScopeLoadingLabel,
  shouldDeferAccessMessage,
  SCOPE_LOADING_HINT,
} from "./utils/accessViewState.js";
import {
  buildNotificationChannels,
  emptyAppData,
  normalizeAlertRouting,
  normalizeSettings,
  resolveDefaultClusterId,
  resolveDisplayUser,
  getUserInitials,
} from "./utils/formatters.js";
import { applyTheme, readThemePreference, storeThemePreference } from "./utils/theme.js";
import { matchPath, pathForPageKey } from "./routes/paths.js";
import RequireAccess from "./routes/RequireAccess.jsx";
import CommandPalette from "./components/search/CommandPalette.jsx";
import { usePageTour } from "./hooks/usePageTour.js";
import { useRequestNotifications } from "./hooks/useRequestNotifications.js";
import { useAttentionSources } from "./dashboard/useAttentionSources.js";
import { useClusterScope } from "./hooks/useClusterScope.js";
import { useNamespaceContext } from "./hooks/useNamespaceContext.js";
import {
  ROUTES,
  navPageKeyFor,
  routeHidesBundleFab,
  routeLoadingLabel,
  routeNeedsClusterContext,
  routeNeedsNamespaceContext,
} from "./routes/routeTable.js";
import CoachMarks from "./components/tour/CoachMarks.jsx";

// Theme is a per-browser preference: the locally stored choice always wins
// over the workspace value returned by the API, so one user's theme never
// changes what teammates see.
const withLocalTheme = (settings) => ({ ...settings, theme: readThemePreference() });

const LoginPage = lazy(() => import("./pages/LoginPage"));
const OnboardingPage = lazy(() => import("./pages/OnboardingPage.jsx"));
const NoFeaturesPage = lazy(() =>
  import("./pages/AccessDeniedPage.jsx").then((module) => ({ default: module.NoFeaturesPage }))
);
const DashboardPage = lazy(() => import("./pages/DashboardPage.jsx"));
const ClustersPage = lazy(() => import("./pages/ClustersPage.jsx"));
const ClusterManagementPage = lazy(() => import("./pages/ClusterManagementPage.jsx"));
const ClusterOverviewPage = lazy(() => import("./pages/ClusterOverviewPage.jsx"));
const InventoryPage = lazy(() => import("./pages/InventoryPage.jsx"));
const ApplicationDetailsRoute = lazy(() => import("./pages/ApplicationDetailsRoute.jsx"));
const NamespacesPage = lazy(() => import("./pages/NamespacesPage.jsx"));
const ResourcesRoute = lazy(() => import("./pages/ResourcesRoute.jsx"));
const TopologyPage = lazy(() => import("./pages/TopologyPage.jsx"));
const LogsPage = lazy(() => import("./pages/LogsPage.jsx"));
const AlertsPage = lazy(() => import("./pages/AlertsPage.jsx"));
const UpgradeSafeModePage = lazy(() => import("./pages/UpgradeSafeModePage.jsx"));
const ClusterBuilderPage = lazy(() => import("./pages/ClusterBuilderPage.jsx"));
const UserManagementPage = lazy(() => import("./pages/UserManagementPage.jsx"));
const AuditLogsPage = lazy(() => import("./pages/AuditLogsPage.jsx"));
const DeploymentRequestsPage = lazy(() => import("./pages/DeploymentRequestsPage.jsx"));
const MyRequestsPage = lazy(() => import("./pages/MyRequestsPage.jsx"));
const ChangeBundlesPage = lazy(() => import("./pages/ChangeBundlesPage.jsx"));
const SettingsPage = lazy(() => import("./pages/SettingsPage.jsx"));
const ImageRegistriesPage = lazy(() => import("./pages/ImageRegistriesPage.jsx"));
const TicketingPage = lazy(() => import("./pages/TicketingPage.jsx"));
const MobileAppsPage = lazy(() => import("./pages/MobileAppsPage.jsx"));
const ApplicationServicesPage = lazy(() => import("./pages/ApplicationServicesPage.jsx"));
const ApplicationIntelligencePage = lazy(() => import("./pages/ApplicationIntelligencePage.jsx"));
const ClientsPage = lazy(() => import("./pages/ClientsPage.jsx"));
const ServiceCatalogPage = lazy(() => import("./pages/ServiceCatalogPage.jsx"));
const ComponentsPage = lazy(() => import("./pages/ComponentsPage.jsx"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage.jsx"));
const IntegrationsHubPage = lazy(() => import("./pages/integrations/IntegrationsHubPage.jsx"));
const IntegrationDetailPage = lazy(() =>
  import("./pages/integrations/IntegrationDetailPage.jsx")
);

/**
 * Renders the matched route's page.
 *
 * The call is deferred into a component rather than passed as
 * `element={renderPage(key)}` so only the matched route's props are built —
 * otherwise every render would construct the element tree for all 27 routes.
 */
function RoutePage({ pageKey, render }) {
  return render(pageKey);
}

export default function App() {
  const {
    user: authUser,
    loading: authLoading,
    isAuthenticated,
    logout,
    hasPermission,
    pageAllowed: isPageAllowed,
    getVisiblePages,
    getFirstAllowedPage,
    getAllowedResources,
    canAccessCluster,
      shouldShowAccessError,
    filterAlertsForUser,
    isAdmin,
    needsOnboarding,
  } = useAuth();
  const changeBundle = useChangeBundle();

  // The floating "Change Bundle" button is fixed to the bottom-right of the
  // viewport, so it sits on top of bottom-right page content (e.g. the data
  // table pager's "Next" button on list pages). Toggle a body class while the
  // button is visible so scrollable pages reserve clearance underneath it.
  const bundleFabVisible = changeBundle.enabled && !changeBundle.isOpen;
  useEffect(() => {
    document.body.classList.toggle("has-bundle-fab", bundleFabVisible);
    return () => document.body.classList.remove("has-bundle-fab");
  }, [bundleFabVisible]);
  // The URL is the single source of truth for which page is open. `activePage`
  // is derived, not stored — the old `useState` copy and the effect that wrote
  // the permission-resolved value back into it are both gone, so there is no
  // longer a render in which the two disagree (ROUTING-AUDIT.md F4).
  //
  // It keeps the same page-key vocabulary the rest of the app already speaks
  // (RBAC, tours, the sidebar), so effects and props below are unchanged.
  const location = useLocation();
  const navigate = useNavigate();
  const routeMatch = useMemo(() => matchPath(location.pathname), [location.pathname]);
  const activePage = routeMatch?.pageKey || "";
  const routeParams = routeMatch?.params;
  // Read-only view of the query string for the few shell-level props that take
  // a value from it. Writing is done by useClusterScope and by the routes.
  const locationSearch = useMemo(
    () => new URLSearchParams(location.search),
    [location.search]
  );

  const [selectedClusterId, setSelectedClusterId] = useState("");
  const [selectedNamespace, setSelectedNamespace] = useState("");
  const [loadingState, setLoadingState] = useState({
    core: false,
    resources: false,
    page: false,
  });
  const [errorState, setErrorState] = useState({ core: "", page: "" });
  const [data, setData] = useState(emptyAppData);
  const [clusterOverview, setClusterOverview] = useState(null);
  // withLocalTheme here too — otherwise the theme effect below persists the
  // "system" default over the user's stored preference before settings load.
  const [settingsDraft, setSettingsDraft] = useState(() =>
    withLocalTheme(normalizeSettings(emptyAppData.settings))
  );
  const [savingRouting, setSavingRouting] = useState(false);
  const [routingError, setRoutingError] = useState("");
  const [testingEmail, setTestingEmail] = useState(false);
  const [testEmailMessage, setTestEmailMessage] = useState("");
  // Decided (approved/declined) deployment requests for the current user, shown
  // in the notifications bell. "Seen" signatures are persisted per user so the
  // badge only counts decisions the user has not opened yet.
  // Apply the selected theme (light/dark/system) to the document. Re-runs on
  // change so the UI updates immediately, and follows the OS when "system".
  useEffect(() => {
    const preference = settingsDraft.theme || "system";
    applyTheme(preference);
    storeThemePreference(preference);
    if (preference === "system" && typeof window !== "undefined" && window.matchMedia) {
      const media = window.matchMedia("(prefers-color-scheme: light)");
      const handler = () => applyTheme("system");
      media.addEventListener("change", handler);
      return () => media.removeEventListener("change", handler);
    }
    return undefined;
  }, [settingsDraft.theme]);

  const alertsLoadRef = useRef({ key: "", at: 0 });

  // Decided deployment requests for the bell. Page-independent by nature, which
  // is why it is extracted rather than rehomed into a route.
  const {
    items: visibleRequestUpdates,
    unseenCount: newRequestCount,
    markAllSeen: markRequestUpdatesSeen,
    dismiss: dismissRequestUpdate,
    dismissAll: clearRequestUpdates,
  } = useRequestNotifications({
    enabled: Boolean(isAuthenticated) && hasPermission("deployment_requests:request"),
    userId: authUser?.id,
  });

  const applyPageError = useCallback((message, { expectedDenied = false } = {}) => {
    if (!shouldShowAccessError(message, { expectedDenied })) {
      setErrorState((prev) => ({ ...prev, page: "" }));
      return;
    }
    setErrorState((prev) => ({ ...prev, page: formatAccessError(message) }));
  }, [shouldShowAccessError]);

  const allowedClusters = useMemo(
    () => data.clusters,
    [data.clusters]
  );
  const selectedCluster = allowedClusters.find((cluster) => cluster.id === selectedClusterId);
  const hasClusters = allowedClusters.length > 0;

  const displayUser = resolveDisplayUser(authUser);

  const visiblePages = useMemo(
    () => getVisiblePages(),
    [getVisiblePages, authUser?.id, authUser?.permissions, authUser?.accessRules]
  );


  /**
   * The page whose data App should fetch, or null.
   *
   * Renamed from `resolvedActivePage`, and the semantics changed with it: it no
   * longer falls back to "the first page you are allowed to see". A page that
   * is not open is not a page to fetch for.
   *
   * Three cases now yield null, and each used to yield the dashboard:
   *   - no route matched (404)
   *   - the route exists but this user may not see it (RequireAccess renders
   *     the denial; fetching its data anyway would be a needless 403 in the
   *     network log and, on cluster-scoped pages, a wasted round-trip)
   *   - the user has no visible pages at all
   *
   * Effects and the tour engine below key on this, so null means "do nothing",
   * which is the honest answer in all three.
   */
  const authorizedPage = useMemo(() => {
    if (!visiblePages.length || !activePage) {
      return null;
    }
    // Drill-down routes (e.g. applicationDetails) are valid but not sidebar entries.
    return isPageAllowed(activePage) ? activePage : null;
  }, [visiblePages, activePage, isPageAllowed]);

  // Namespaces come from the loader hook rather than the shared `data` blob:
  // they are cluster scope, not page data, and every cluster-scoped screen
  // needs them regardless of which one is open.
  const handleNamespaceError = useCallback(
    (error) => {
      applyPageError(error?.message, {
        expectedDenied: !canAccessCluster(selectedClusterId),
      });
    },
    [applyPageError, canAccessCluster, selectedClusterId]
  );

  const { namespaces: allowedNamespaces, loading: namespacesLoading } = useNamespaceContext({
    clusterId: selectedClusterId,
    enabled: Boolean(isAuthenticated) && routeNeedsClusterContext(authorizedPage),
    onError: handleNamespaceError,
  });

  const hasNamespaces = allowedNamespaces.length > 0;

  // Two-way binding between the topbar selectors and the URL, so a shared link
  // opens on the scope the sender was looking at and back restores it.
  const { setCluster: handleClusterChange, setNamespace: handleNamespaceChange } =
    useClusterScope({
      pageKey: activePage,
      routeParams,
      clusters: allowedClusters,
      namespaces: allowedNamespaces,
      defaultClusterId: settingsDraft?.defaultCluster,
      selectedClusterId,
      selectedNamespace,
      onClusterChange: setSelectedClusterId,
      onNamespaceChange: setSelectedNamespace,
    });

  // Feed inputs for the dashboard's "needs attention" list. Fetched only there,
  // and only for sources this user may see.
  const {
    integrations: attentionIntegrations,
    approvals: attentionApprovals,
    unavailable: attentionUnavailable,
  } = useAttentionSources({
    enabled: authorizedPage === "dashboard",
    canViewIntegrations: isPageAllowed("integrations"),
    canViewApprovals: hasPermission("deployment_requests:view"),
  });

  // Resource loading is the route's own business now; the shell's overlay
  // tracks cluster and namespace scope only.
  const resourcesLoading = false;
  const scopeDataLoading = namespacesLoading;


  const applyCoreError = (message, { expectedDenied = false } = {}) => {
    if (!shouldShowAccessError(message, { expectedDenied })) {
      setErrorState((prev) => ({ ...prev, core: "" }));
      return;
    }
    setErrorState((prev) => ({ ...prev, core: formatAccessError(message) }));
  };

  const fetchSettings = async () => {
    if (!hasPermission("settings:view")) {
      return normalizeSettings(emptyAppData.settings);
    }
    try {
      return normalizeSettings(await getSettings());
    } catch {
      return normalizeSettings(emptyAppData.settings);
    }
  };

  // Same signature the ~8 existing call sites already use, so they are
  // unchanged; only the mechanism moved from state to the URL.
  //
  // The permission check stays here for now. It becomes a route-level guard in
  // the next step, at which point a denied page renders AccessDeniedPage at its
  // own URL instead of the click silently doing nothing.
  const handleNavigate = useCallback(
    (pageKey, params) => {
      if (!isPageAllowed(pageKey)) {
        return;
      }
      const path = pathForPageKey(pageKey, params);
      if (path) {
        navigate(path);
      }
    },
    [isPageAllowed, navigate]
  );

  // Guided page tips. Keyed by page key, which is why this came out last: it
  // needed every route to have a stable one.
  const {
    activeTour,
    start: startPageTour,
    close: closeTour,
    mute: muteTours,
  } = usePageTour({
    pageKey: authorizedPage,
    userId: authUser?.id,
    enabled: Boolean(isAuthenticated) && !authLoading && !needsOnboarding,
    isAdmin,
    hasPermission,
    pageAllowed: isPageAllowed,
  });

  // Landing only. "/" is the dashboard, which not every role may open, and a
  // user who typed no URL at all should not be met with a denial for a page
  // they did not ask for — so signing in still lands on the first page they can
  // see, exactly as before.
  //
  // Every other address is left alone. A URL the user deliberately opened and
  // may not see is answered by RequireAccess at that URL, not by a redirect
  // that makes a correct link look broken.
  useEffect(() => {
    if (!isAuthenticated || authLoading) {
      return;
    }
    if (activePage !== "dashboard" || isPageAllowed("dashboard")) {
      return;
    }
    const target = getFirstAllowedPage();
    if (!target || target === "dashboard") {
      return;
    }
    const path = pathForPageKey(target);
    if (path) {
      // replace: the landing page should not sit in history as a back-button
      // trap that re-redirects on every press.
      navigate(path, { replace: true });
    }
  }, [isAuthenticated, authLoading, activePage, getFirstAllowedPage, isPageAllowed, navigate]);

  // The cluster/namespace validation effects that used to live here are gone.
  // useClusterScope resolves both against the lists the user can actually
  // reach, in one place, with the URL as an input — so an invalid selection is
  // corrected by the same rule whether it came from a stale bookmark, a
  // dropdown, or a cluster being deleted out from under a tab.
  useEffect(() => {
    if (isAuthenticated && !allowedClusters.length && selectedClusterId) {
      setSelectedClusterId("");
    }
  }, [isAuthenticated, allowedClusters.length, selectedClusterId]);

  const applyClusterList = (clusters, preferredId) => {
    const filtered = clusters;
    const firstCluster = resolveDefaultClusterId(filtered, preferredId);
    setData((prev) => ({ ...prev, clusters: filtered }));
    setSelectedClusterId((current) =>
      filtered.some((cluster) => cluster.id === current) ? current : firstCluster
    );
    return { filtered, firstCluster };
  };

  const reloadClusters = async () => {
    const [clustersRes, settingsRes] = await Promise.all([listClusters(), fetchSettings()]);
    const clusters = clustersRes.items || [];
    const { firstCluster } = applyClusterList(clusters, settingsRes.defaultCluster);
    setData((prev) => ({
      ...prev,
      settings: withLocalTheme(normalizeSettings({ ...settingsRes, defaultCluster: firstCluster })),
    }));
  };

  // Pick initial cluster only from clusters the user can actually reach (API list ∩ RBAC).
  // Do not restore legacy profile IDs like prod-us-east when the live cluster is docker-desktop.
  //
  // Only for routes that take no cluster. On a cluster-scoped route
  // useClusterScope resolves the selection from the URL and owns it outright;
  // seeding a different cluster here first would fire one wasted namespace load
  // for it before the URL's cluster won. Unscoped routes still need a selection
  // so the topbar alert badge has something to count on a cold load.
  useEffect(() => {
    if (!isAuthenticated || !authUser || selectedClusterId || loadingState.core) {
      return;
    }
    if (!allowedClusters.length || routeNeedsClusterContext(activePage)) {
      return;
    }
    const preferred = settingsDraft?.defaultCluster;
    setSelectedClusterId(resolveDefaultClusterId(allowedClusters, preferred));
  }, [
    isAuthenticated,
    authUser,
    activePage,
    selectedClusterId,
    allowedClusters,
    settingsDraft.defaultCluster,
    loadingState.core,
  ]);

  useEffect(() => {
    if (!isAuthenticated) {
      return undefined;
    }
    let cancelled = false;
    const loadCoreData = async () => {
      setLoadingState((prev) => ({ ...prev, core: true }));
      setErrorState((prev) => ({ ...prev, core: "" }));

      try {
        const [clustersRes, settingsRes] = await Promise.all([listClusters(), fetchSettings()]);
        if (cancelled) {
          return;
        }

        const clusters = clustersRes.items || [];
        const { filtered, firstCluster } = applyClusterList(clusters, settingsRes.defaultCluster);
        const normalizedSettings = withLocalTheme(
          normalizeSettings({
            ...settingsRes,
            defaultCluster: firstCluster,
          })
        );
        setData((prev) => ({
          ...prev,
          clusters: filtered,
          alerts: [],
          alertsMeta: {},
          settings: normalizedSettings,
          notificationChannels: buildNotificationChannels(normalizedSettings),
        }));
        setSettingsDraft(normalizedSettings);
      } catch (coreError) {
        if (!cancelled) {
          applyCoreError(coreError.message);
        }
      } finally {
        if (!cancelled) {
          setLoadingState((prev) => ({ ...prev, core: false }));
        }
      }
    };

    loadCoreData();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, authUser?.id]);

  // The cluster overview is its own fetch now. It used to ride along inside the
  // namespace loader's Promise.allSettled to save a round-trip, but the two
  // already ran in parallel, so separating them costs nothing and stops a
  // namespace loader from having to know which page is open.
  useEffect(() => {
    if (!isAuthenticated || !selectedClusterId || authorizedPage !== "clusterOverview") {
      return undefined;
    }
    let cancelled = false;
    (async () => {
      try {
        const overview = await getClusterOverview(selectedClusterId);
        if (!cancelled) {
          setClusterOverview(overview);
        }
      } catch (loadError) {
        if (!cancelled) {
          applyPageError(loadError.message, {
            expectedDenied: !canAccessCluster(selectedClusterId),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, selectedClusterId, authorizedPage]);

  useEffect(() => {
    if (!selectedClusterId) {
      setData((prev) => ({ ...prev, alerts: [], alertsMeta: {} }));
      return;
    }

    // The dashboard used to be skipped here, because the summary carried its
    // own server-side alert total for the badge. The badge reads one source
    // now, so skipping would leave the bell empty on the one page people watch
    // it from. The 30s dedupe below already stops this costing a request per
    // navigation.
    if (!hasPermission("alerts:view") || !canAccessCluster(selectedClusterId)) {
      setData((prev) => ({ ...prev, alerts: [], alertsMeta: {} }));
      return;
    }

    // Page switches re-run this effect; skip the network round-trip when the
    // same cluster/user combination was fetched moments ago.
    const alertsKey = `${selectedClusterId}|${authUser?.id || ""}`;
    const lastLoad = alertsLoadRef.current;
    if (lastLoad.key === alertsKey && Date.now() - lastLoad.at < 30000) {
      return;
    }

    let cancelled = false;
    const loadAlerts = async () => {
      try {
        const alertsRes = await listAlerts({ cluster: selectedClusterId });
        alertsLoadRef.current = { key: alertsKey, at: Date.now() };
        if (!cancelled) {
          const filteredAlerts = filterAlertsForUser(alertsRes.items || []);
          setData((prev) => ({
            ...prev,
            alerts: filteredAlerts,
            alertsMeta: alertsRes.metadata || {},
            notificationChannels: hasPermission("alerts:manage")
              ? buildNotificationChannels(prev.settings)
              : [],
          }));
        }
      } catch (loadError) {
        if (!cancelled) {
          setData((prev) => ({
            ...prev,
            alerts: [],
            alertsMeta: {
              mode: "real",
              source: "none",
              reason: shouldShowAccessError(loadError.message) ? loadError.message : "",
            },
          }));
        }
      }
    };

    loadAlerts();
    return () => {
      cancelled = true;
    };
  }, [
    selectedClusterId,
    authUser?.id,
    hasPermission,
    canAccessCluster,
    filterAlertsForUser,
    authorizedPage,
  ]);

  const testAlertEmailDelivery = async (routing) => {
    setTestingEmail(true);
    setTestEmailMessage("");
    setRoutingError("");
    try {
      await saveAlertRouting(routing);
      const result = await testAlertEmail();
      setTestEmailMessage(result.message || "Test email sent.");
    } catch (testError) {
      setRoutingError(testError.message);
    } finally {
      setTestingEmail(false);
    }
  };

  const saveAlertRouting = async (routing) => {
    setSavingRouting(true);
    setRoutingError("");
    try {
      const hasEnabledChannel = Object.values(routing).some((channel) => channel.enabled);
      const notifications = {
        ...settingsDraft.notifications,
        routing: normalizeAlertRouting(routing),
        alerts: hasEnabledChannel,
      };
      await updateSettings({ notifications });
      const refreshedSettings = withLocalTheme(normalizeSettings(await getSettings()));
      setData((prev) => ({
        ...prev,
        settings: refreshedSettings,
        notificationChannels: buildNotificationChannels(refreshedSettings),
      }));
      setSettingsDraft(refreshedSettings);
      return true;
    } catch (routingSaveError) {
      setRoutingError(routingSaveError.message);
      return false;
    } finally {
      setSavingRouting(false);
    }
  };

  const saveSettings = async () => {
    setLoadingState((prev) => ({ ...prev, page: true }));
    try {
      const payload = {
        ...settingsDraft,
        refreshIntervalSeconds: Number(settingsDraft.refreshIntervalSeconds) || 30,
      };
      await updateSettings(payload);
      const refreshedSettings = withLocalTheme(normalizeSettings(await getSettings()));
      setData((prev) => ({
        ...prev,
        settings: refreshedSettings,
        notificationChannels: buildNotificationChannels(refreshedSettings),
      }));
      setSettingsDraft(refreshedSettings);
      if (refreshedSettings.defaultCluster) {
        setSelectedClusterId(refreshedSettings.defaultCluster);
      }
      setErrorState((prev) => ({ ...prev, page: "Settings saved successfully." }));
    } catch (settingsError) {
      applyPageError(settingsError.message);
    } finally {
      setLoadingState((prev) => ({ ...prev, page: false }));
    }
  };

  // Revert unsaved draft edits back to the last saved settings. The theme is
  // kept: it is a per-browser preference that applies immediately and is not
  // part of the dirty/save flow.
  const discardSettingsDraft = () => {
    setSettingsDraft((prev) => ({ ...normalizeSettings(data.settings), theme: prev.theme }));
  };

  const handleSettingsDraftChange = (key, value) => {
    setSettingsDraft((prev) => {
      if (key === "notifications.alerts") {
        return {
          ...prev,
          notifications: { ...prev.notifications, alerts: Boolean(value) },
        };
      }
      if (key === "notifications.upgrades") {
        return {
          ...prev,
          notifications: { ...prev.notifications, upgrades: Boolean(value) },
        };
      }
      if (key === "refreshIntervalSeconds") {
        return { ...prev, refreshIntervalSeconds: Number(value) || 30 };
      }
      return { ...prev, [key]: value };
    });
  };

  const activeClusterLabel =
    allowedClusters.find((cluster) => cluster.id === selectedClusterId)?.name || selectedClusterId || "";

  // Resource lists belong to the route that shows them; ResourcesRoute owns the
  // namespace cache now. The key stays so `scopedData` keeps its shape for the
  // pages that spread it, but nothing in the shell can populate it.
  const allowedResources = emptyNamespaceResources();

  const scopedData = {
    ...data,
    clusters: allowedClusters,
    namespaces: allowedNamespaces,
    resources: allowedResources,
  };

  const pageAccessError = errorState.page || errorState.core;

  const renderPage = (pageKey) => {
    switch (pageKey) {
      case "dashboard":
        return (
          <DashboardPage
            coreLoading={loadingState.core}
            accessError={errorState.core}
            hasClusters={hasClusters}
            selectedCluster={selectedCluster}
            refreshIntervalSeconds={settingsDraft.refreshIntervalSeconds}
            onClusterMissing={() => reloadClusters().catch(() => {})}
            onNavigateToUpgrade={() => handleNavigate("upgrade")}
            onNavigateToInventory={() => handleNavigate("inventory")}
            canOpenUpgrade={isPageAllowed("upgrade")}
            canOpenInventory={isPageAllowed("inventory")}
            alerts={data.alerts}
            integrations={attentionIntegrations}
            approvals={attentionApprovals}
            unavailableSources={attentionUnavailable}
          />
        );
      case "clusters":
        return (
          <ClustersPage
            data={scopedData}
            hasClusters={hasClusters}
            coreLoading={loadingState.core}
            accessError={pageAccessError}
          />
        );
      case "clusterManagement":
        return (
          <ClusterManagementPage
            onClustersChanged={reloadClusters}
            canAdd={hasPermission("clusters:add")}
            canUpdate={hasPermission("clusters:update")}
            canRemove={hasPermission("clusters:remove")}
            canTest={hasPermission("clusters:test")}
          />
        );
      case "clusterOverview":
        return (
          <ClusterOverviewPage
            cluster={selectedCluster}
            overview={clusterOverview}
            namespaces={allowedNamespaces}
            hasClusters={hasClusters}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
      case "inventory":
        return (
          <InventoryPage
            coreLoading={loadingState.core}
            accessError={pageAccessError}
            hasClusters={hasClusters}
            allowedClusters={allowedClusters}
            defaultClusterId={selectedClusterId}
            canRegister={hasPermission("inventory:register")}
            canDeploy={hasPermission("apps:deploy")}
            canHelmView={hasPermission("helm:view")}
            canHelmInstall={hasPermission("helm:install")}
            canManageTemplates={hasPermission("inventory:update")}
            isAdmin={isAdmin}
          />
        );
      case "applicationDetails":
        return <ApplicationDetailsRoute allowedClusters={allowedClusters} />;
      case "namespaces":
        return (
          <NamespacesPage
            data={scopedData}
            hasClusters={hasClusters}
            hasNamespaces={hasNamespaces}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
      case "resources":
        return (
          <ResourcesRoute
            clusterId={selectedClusterId}
            namespace={selectedNamespace}
            scopedData={scopedData}
            hasClusters={hasClusters}
            hasNamespaces={hasNamespaces}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
      case "topology":
        return (
          <TopologyPage
            clusterId={selectedClusterId}
            cluster={selectedCluster}
            hasClusters={hasClusters}
            coreLoading={loadingState.core}
            accessError={pageAccessError}
          />
        );
      case "logs":
        return (
          <LogsPage
            clusters={allowedClusters}
            namespaces={allowedNamespaces}
            selectedClusterId={selectedClusterId}
            selectedNamespace={selectedNamespace}
            preferredPod={locationSearch.get("pod") || ""}
            onPreferredPodApplied={() => {
              // The prefill is a one-shot: once Logs has selected that pod, the
              // param must go, or every later cluster/namespace change would
              // snap the selection back to it.
              if (!locationSearch.has("pod")) {
                return;
              }
              const next = new URLSearchParams(locationSearch);
              next.delete("pod");
              navigate(`${location.pathname}?${next.toString()}`, { replace: true });
            }}
            onClusterChange={handleClusterChange}
            onNamespaceChange={handleNamespaceChange}
            hasClusters={hasClusters}
            hasNamespaces={hasNamespaces}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
      case "alerts":
        return (
          <AlertsPage
            data={scopedData}
            selectedClusterId={selectedClusterId}
            allowedClusters={allowedClusters}
            allowedNamespaces={allowedNamespaces}
            selectedNamespace={selectedNamespace}
            canManageAlerts={hasPermission("alerts:manage")}
            hasClusters={hasClusters}
            authUser={authUser}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
      case "imageRegistries":
        return <ImageRegistriesPage canManage={hasPermission("registries:manage")} />;
      case "ticketing":
        return <TicketingPage canManage={hasPermission("ticketing:manage")} />;
      case "mobileApps":
        return <MobileAppsPage canManage={hasPermission("mobile_apps:manage")} canPublish={isAdmin} />;
      case "clusterBuilder":
        return (
          <ClusterBuilderPage
            canCreate={hasPermission("cluster_builds:create")}
            canExecute={hasPermission("cluster_builds:execute")}
            canDownloadKubeconfig={hasPermission("cluster_builds:kubeconfig")}
            canManageVSphere={hasPermission("vsphere:manage")}
            canManageSSH={hasPermission("ssh_credentials:manage")}
            canManageBuildProfiles={hasPermission("cluster_builds:create")}
            // A finished build can hand off to the cluster it produced: select
            // it so the Clusters page opens already scoped to it.
            onOpenCluster={
              isPageAllowed("clusters")
                ? (clusterId) => {
                  if (clusterId) setSelectedClusterId(clusterId);
                  handleNavigate("clusters");
                }
                : null
            }
          />
        );
      case "upgrade":
        return (
          <UpgradeSafeModePage
            clusterId={selectedClusterId}
            clusterLabel={activeClusterLabel}
            onViewInstructions={() =>
              document.querySelector(".upgrade-instructions")?.scrollIntoView({ behavior: "smooth" })
            }
            coreLoading={loadingState.core}
            hasClusters={hasClusters}
            accessError={errorState.core}
            canPrecheck={hasPermission("upgrades:precheck") && canAccessCluster(selectedClusterId)}
            canStart={hasPermission("upgrades:start") && canAccessCluster(selectedClusterId)}
          />
        );
      case "serviceCatalog":
        return <ServiceCatalogPage clusters={allowedClusters} />;
      case "applicationServices":
        return <ApplicationServicesPage clusters={allowedClusters} />;
      case "applicationIntelligence":
        return (
          <ApplicationIntelligencePage
            clusters={allowedClusters}
            canManage={hasPermission("applications:manage")}
            canAnalyze={hasPermission("applications:analyze")}
          />
        );
      case "components":
        return <ComponentsPage />;
      case "clients":
        return <ClientsPage clusters={allowedClusters} />;
      case "userManagement":
        return <UserManagementPage clusters={allowedClusters} />;
      case "auditLogs":
        return <AuditLogsPage />;
      case "deploymentRequests":
        return <DeploymentRequestsPage />;
      case "myRequests":
        return <MyRequestsPage />;
      case "changeBundles":
        return <ChangeBundlesPage />;
      case "integrations":
        return <IntegrationsHubPage />;
      // The four detail tabs are separate routes sharing one component, so a
      // tab is a shareable address rather than component state.
      case "integrationDetail":
        return <IntegrationDetailPage tab="overview" />;
      case "integrationConfiguration":
        return <IntegrationDetailPage tab="configuration" />;
      case "integrationActivity":
        return <IntegrationDetailPage tab="activity" />;
      case "integrationUsedBy":
        return <IntegrationDetailPage tab="usedBy" />;
      case "settings":
        return (
          <SettingsPage
            data={{ ...data, user: displayUser }}
            clusters={allowedClusters}
            settingsDraft={settingsDraft}
            onSettingsChange={handleSettingsDraftChange}
            onSave={saveSettings}
            onDiscard={discardSettingsDraft}
            saving={loadingState.page}
            canManage={hasPermission("settings:manage")}
            authUser={authUser}
            onNavigate={handleNavigate}
            isPageAllowed={isPageAllowed}
            hasPermission={hasPermission}
          />
        );
      default:
        // Reached only if a route exists with no render case — a table/switch
        // mismatch, which routeTable.test.js guards against. Previously this
        // arm silently re-rendered the dashboard for any unknown key (F1).
        return <NotFoundPage />;
    }
  };

  let pageNode = null;
  if (!visiblePages.length) {
    pageNode = (
      <Suspense fallback={<RouteLoadingFallback label="Loading..." />}>
        <NoFeaturesPage />
      </Suspense>
    );
  } else {
    pageNode = (
      <Suspense fallback={<RouteLoadingFallback label={routeLoadingLabel(activePage)} />}>
        <Routes>
          {ROUTES.map((route) => (
            <Route
              key={route.pageKey}
              path={route.path}
              element={
                <RequireAccess pageKey={route.pageKey} isPageAllowed={isPageAllowed}>
                  <RoutePage pageKey={route.pageKey} render={renderPage} />
                </RequireAccess>
              }
            />
          ))}
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Suspense>
    );
  }

  // One source. The badge used to read the dashboard summary's server-side
  // total on the dashboard and the RBAC-filtered list everywhere else, so the
  // same bell showed two different numbers depending on which page you were on.
  // The filtered list is the honest one — it counts what this user may actually
  // open — and it is now used everywhere.
  const alertBadgeCount = Array.isArray(data.alerts) ? data.alerts.length : 0;

  if (authLoading) {
    return (
      <div className="login-screen">
        <p className="muted">Loading session...</p>
      </div>
    );
  }

  // First-login setup (password change + MFA enrolment) blocks all dashboard
  // access until complete. This runs before the authenticated check because the
  // user does not hold a full session token yet.
  if (needsOnboarding) {
    return (
      <Suspense fallback={<RouteLoadingFallback label="Loading setup..." />}>
        <OnboardingPage />
      </Suspense>
    );
  }

  if (!isAuthenticated) {
    return (
      <Suspense fallback={<RouteLoadingFallback label="Loading sign in..." />}>
        <LoginPage />
      </Suspense>
    );
  }

  const userInitials = getUserInitials(displayUser.name);

  const deferGlobalMessages = shouldDeferAccessMessage({
    coreLoading: loadingState.core,
    pageLoading: loadingState.page,
    namespacesLoading,
    resourcesLoading,
  });

  // Show the "no clusters assigned" banner only where the screen actually takes
  // a cluster. This used to be a hardcoded list of four page keys that was never
  // extended as pages were added, so Ticketing, Change Bundles, Clients and
  // Components all nagged about missing clusters they never use (F-list, §D).
  const clusterBanner =
    !deferGlobalMessages &&
    isAuthenticated &&
    !hasClusters &&
    routeNeedsClusterContext(activePage)
      ? EMPTY_MESSAGES.noClusters
      : "";

  const globalErrorMessage = deferGlobalMessages ? "" : errorState.core;

  const showLoadingOverlay = loadingState.core;
  const loadingOverlayLabel = getScopeLoadingLabel({
    coreLoading: loadingState.core,
    namespacesLoading,
    resourcesLoading,
    pageLoading: false,
  });
  const loadingOverlayHint =
    namespacesLoading || resourcesLoading ? SCOPE_LOADING_HINT : undefined;

  return (
    <>
    <AppShell
      visiblePages={visiblePages}
      activePage={navPageKeyFor(activePage)}
      onNavigate={handleNavigate}
      allowedClusters={allowedClusters}
      allowedNamespaces={allowedNamespaces}
      selectedClusterId={selectedClusterId}
      selectedNamespace={selectedNamespace}
      onClusterChange={handleClusterChange}
      onNamespaceChange={handleNamespaceChange}
      loadingCore={loadingState.core}
      loadingNamespaces={namespacesLoading}
      loadingResources={resourcesLoading}
      loadingPage={scopeDataLoading || loadingState.page}
      loadingOverlay={showLoadingOverlay}
      loadingOverlayLabel={loadingOverlayLabel}
      loadingOverlayHint={loadingOverlayHint}
      errorMessage={globalErrorMessage}
      clusterBannerMessage={clusterBanner}
      showClusterSelector={routeNeedsClusterContext(activePage)}
      showNamespaceSelector={routeNeedsNamespaceContext(activePage)}
      alertBadgeCount={alertBadgeCount}
      notifications={data.alerts}
      clusterLabel={activeClusterLabel}
      canViewAlerts={hasPermission("alerts:view")}
      notificationsEnabled={data.settings?.notifications?.alerts !== false}
      onViewAllAlerts={() => handleNavigate("alerts")}
      requestUpdates={visibleRequestUpdates}
      canViewRequests={hasPermission("deployment_requests:request")}
      requestBadgeCount={newRequestCount}
      onViewAllRequests={() => handleNavigate("myRequests")}
      onNotificationsOpen={markRequestUpdatesSeen}
      onDismissRequestUpdate={dismissRequestUpdate}
      onClearRequestUpdates={clearRequestUpdates}
      displayUser={displayUser}
      userInitials={userInitials}
      onLogout={logout}
      onStartTour={startPageTour}
    >
      {pageNode}
    </AppShell>
    <CommandPalette
      visiblePages={visiblePages}
      clusters={allowedClusters}
      namespaces={allowedNamespaces}
      clusterId={selectedClusterId}
    />
    {activeTour ? (
      <CoachMarks
        steps={activeTour.steps}
        showMuteOption={activeTour.auto}
        onFinish={closeTour}
        onDismiss={closeTour}
        onMuteAuto={muteTours}
      />
    ) : null}
    {changeBundle.enabled ? (
      <>
        {!changeBundle.isOpen && !routeHidesBundleFab(activePage) ? (
        <button
          type="button"
          aria-label="Open change bundle"
          onClick={changeBundle.openDrawer}
          className="fab"
        >
          Change Bundle
          {changeBundle.itemCount > 0 ? (
            <span
              style={{
                background: "var(--bg-main)",
                color: "var(--accent-strong)",
                borderRadius: 999,
                minWidth: 20,
                height: 20,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "0.75rem",
                padding: "0 6px",
              }}
            >
              {changeBundle.itemCount}
            </span>
          ) : null}
        </button>
        ) : null}
        <ChangeBundleDrawer />
      </>
    ) : null}
    </>
  );
}
