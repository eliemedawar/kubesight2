import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  getClusterOverview,
  getDashboardSummary,
  getInventoryDetail,
  listInventory,
  getSettings,
  listAlerts,
  listMyDeploymentRequests,
  listClusters,
  testAlertEmail,
  listNamespacesByCluster,
  getUpgradeInfo,
  getUpgradeJob,
  runUpgradePrecheck,
  startUpgrade,
  updateSettings,
} from "./api";
import { useAuth } from "./context/AuthContext";
import { useChangeBundle } from "./context/ChangeBundleContext";
import ChangeBundleDrawer from "./components/changes/ChangeBundleDrawer.jsx";
import AppShell from "./components/layout/AppShell.jsx";
import RouteLoadingFallback from "./components/common/RouteLoadingFallback.jsx";
import { emptyNamespaceResources, listKeyForTab } from "./lib/resourceTypes.js";
import { useNamespaceResourceCache } from "./hooks/useNamespaceResourceCache.js";
import { resourceCache } from "./services/resourceCacheService.js";
import {
  EMPTY_MESSAGES,
  formatAccessError,
  pageNeedsClusterContext,
  pageNeedsNamespaceContext,
} from "./utils/authz.js";
import {
  getScopeLoadingLabel,
  pageNeedsPodsData,
  pageNeedsResourceData,
  pageNeedsResourceTabs,
  shouldDeferAccessMessage,
  SCOPE_LOADING_HINT,
} from "./utils/accessViewState.js";
import { removeFromInventory, updateCatalogEntry } from "./api/inventoryApi.js";
import {
  buildNotificationChannels,
  emptyAppData,
  mapPrecheckState,
  normalizeAlertRouting,
  normalizeSettings,
  resolveDefaultClusterId,
  resolveDisplayUser,
  getUserInitials,
} from "./utils/formatters.js";
import { applyTheme, storeThemePreference } from "./utils/theme.js";

const LoginPage = lazy(() => import("./pages/LoginPage"));
const NoFeaturesPage = lazy(() =>
  import("./pages/AccessDeniedPage.jsx").then((module) => ({ default: module.NoFeaturesPage }))
);
const DashboardPage = lazy(() => import("./pages/DashboardPage.jsx"));
const ClustersPage = lazy(() => import("./pages/ClustersPage.jsx"));
const ClusterManagementPage = lazy(() => import("./pages/ClusterManagementPage.jsx"));
const ClusterOverviewPage = lazy(() => import("./pages/ClusterOverviewPage.jsx"));
const InventoryPage = lazy(() => import("./pages/InventoryPage.jsx"));
const ApplicationDetailsPage = lazy(() => import("./pages/ApplicationDetailsPage.jsx"));
const NamespacesPage = lazy(() => import("./pages/NamespacesPage.jsx"));
const ResourcesPage = lazy(() => import("./pages/ResourcesPage.jsx"));
const LogsPage = lazy(() => import("./pages/LogsPage.jsx"));
const AlertsPage = lazy(() => import("./pages/AlertsPage.jsx"));
const AlertPoliciesPage = lazy(() => import("./pages/AlertPoliciesPage.jsx"));
const AlertRoutingPage = lazy(() => import("./pages/AlertRoutingPage.jsx"));
const UpgradeSafeModePage = lazy(() => import("./pages/UpgradeSafeModePage.jsx"));
const UserManagementPage = lazy(() => import("./pages/UserManagementPage.jsx"));
const AuditLogsPage = lazy(() => import("./pages/AuditLogsPage.jsx"));
const DeploymentRequestsPage = lazy(() => import("./pages/DeploymentRequestsPage.jsx"));
const MyRequestsPage = lazy(() => import("./pages/MyRequestsPage.jsx"));
const ChangeBundlesPage = lazy(() => import("./pages/ChangeBundlesPage.jsx"));
const SettingsPage = lazy(() => import("./pages/SettingsPage.jsx"));
const EditCatalogModal = lazy(() => import("./components/inventory/EditCatalogModal.jsx"));
const ApplicationServicesPage = lazy(() => import("./pages/ApplicationServicesPage.jsx"));
const ClientsPage = lazy(() => import("./pages/ClientsPage.jsx"));

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
    getVisibleResourceTabs,
    shouldShowAccessError,
    filterAlertsForUser,
    isAdmin,
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
  const [activePage, setActivePage] = useState("dashboard");
  const [selectedClusterId, setSelectedClusterId] = useState("");
  const [selectedNamespace, setSelectedNamespace] = useState("");
  const [loadingState, setLoadingState] = useState({
    core: false,
    namespaces: false,
    resources: false,
    page: false,
  });
  const [errorState, setErrorState] = useState({ core: "", page: "" });
  const [data, setData] = useState(emptyAppData);
  const [clusterOverview, setClusterOverview] = useState(null);
  const [upgradeResult, setUpgradeResult] = useState(null);
  const [targetVersion, setTargetVersion] = useState("v1.31.0");
  const [confirmationText, setConfirmationText] = useState("");
  const [dashboardSummary, setDashboardSummary] = useState(null);
  const [dashboardRefreshedAt, setDashboardRefreshedAt] = useState(null);
  const [inventoryItems, setInventoryItems] = useState([]);
  const [selectedApplicationId, setSelectedApplicationId] = useState("");
  const [applicationDetailsTab, setApplicationDetailsTab] = useState("overview");
  const [applicationDetail, setApplicationDetail] = useState(null);
  const [editCatalogOpen, setEditCatalogOpen] = useState(false);
  const [editCatalogSaving, setEditCatalogSaving] = useState(false);
  const [editCatalogError, setEditCatalogError] = useState("");
  const [settingsDraft, setSettingsDraft] = useState(normalizeSettings(emptyAppData.settings));
  const [savingRouting, setSavingRouting] = useState(false);
  const [routingError, setRoutingError] = useState("");
  const [testingEmail, setTestingEmail] = useState(false);
  const [testEmailMessage, setTestEmailMessage] = useState("");
  const [preferredLogPod, setPreferredLogPod] = useState("");
  const [resourceActiveTab, setResourceActiveTab] = useState("pods");
  // Decided (approved/declined) deployment requests for the current user, shown
  // in the notifications bell. "Seen" signatures are persisted per user so the
  // badge only counts decisions the user has not opened yet.
  const [requestUpdates, setRequestUpdates] = useState([]);
  const [seenRequestSignatures, setSeenRequestSignatures] = useState(() => new Set());
  const [dismissedRequestSignatures, setDismissedRequestSignatures] = useState(() => new Set());
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

  const applicationDetailRequestRef = useRef(0);
  const clusterContextClusterRef = useRef("");
  const upgradeLoadClusterRef = useRef("");
  const dashboardLoadClusterRef = useRef("");
  const dashboardRequestSeqRef = useRef(0);
  const dashboardSummaryClusterRef = useRef("");
  const [dashboardRefreshing, setDashboardRefreshing] = useState(false);

  const allowedClusters = useMemo(
    () => data.clusters,
    [data.clusters]
  );
  const allowedNamespaces = useMemo(
    () => data.namespaces,
    [data.namespaces]
  );
  const selectedCluster = allowedClusters.find((cluster) => cluster.id === selectedClusterId);
  const hasClusters = allowedClusters.length > 0;
  const hasNamespaces = allowedNamespaces.length > 0;

  const namespacesLoading = loadingState.namespaces;

  const displayUser = resolveDisplayUser(authUser);

  const visiblePages = useMemo(
    () => getVisiblePages(),
    [getVisiblePages, authUser?.id, authUser?.permissions, authUser?.accessRules]
  );

  const visibleResourceTabs = useMemo(() => getVisibleResourceTabs(), [getVisibleResourceTabs, authUser?.id]);

  useEffect(() => {
    if (visibleResourceTabs.length && !visibleResourceTabs.includes(resourceActiveTab)) {
      setResourceActiveTab(visibleResourceTabs[0] || "pods");
    }
  }, [visibleResourceTabs, resourceActiveTab]);

  const resolvedActivePage = useMemo(() => {
    if (!visiblePages.length) {
      return null;
    }
    // Drill-down routes (e.g. applicationDetails) are valid but not sidebar entries.
    if (isPageAllowed(activePage)) {
      return activePage;
    }
    if (visiblePages.some((page) => page.key === activePage)) {
      return activePage;
    }
    return getFirstAllowedPage() || visiblePages[0]?.key || null;
  }, [visiblePages, activePage, isPageAllowed, getFirstAllowedPage]);

  const resourceCacheEnabled =
    pageNeedsResourceData(resolvedActivePage) &&
    Boolean(selectedClusterId && selectedNamespace);

  const activeResourceListKey = useMemo(() => {
    if (!resourceCacheEnabled) {
      return "";
    }
    if (pageNeedsPodsData(resolvedActivePage)) {
      return "pods";
    }
    if (pageNeedsResourceTabs(resolvedActivePage)) {
      return listKeyForTab(resourceActiveTab);
    }
    return "";
  }, [resourceCacheEnabled, resolvedActivePage, resourceActiveTab]);

  const {
    resources: cachedResources,
    rawResources: cachedRawResources,
    refreshTab: refreshResourceTab,
    isTabLoading,
    isTabRefreshing,
    isTabLoaded,
    tabErrors: resourceTabErrors,
    activeTabLoading,
    activeTabRefreshing,
  } = useNamespaceResourceCache({
    clusterId: selectedClusterId,
    namespace: selectedNamespace,
    activeListKey: activeResourceListKey,
    enabled: resourceCacheEnabled,
    filterResources: getAllowedResources,
  });

  const allowedResources = useMemo(
    () => (resourceCacheEnabled ? cachedResources : emptyNamespaceResources()),
    [resourceCacheEnabled, cachedResources]
  );

  const resourcesLoading = resourceCacheEnabled && activeTabLoading;
  const scopeDataLoading = namespacesLoading || resourcesLoading;

  const isDashboardPage = resolvedActivePage === "dashboard";

  useEffect(() => {
    if (resolvedActivePage && resolvedActivePage !== activePage) {
      setActivePage(resolvedActivePage);
    }
  }, [resolvedActivePage, activePage]);

  const applyPageError = (message, { expectedDenied = false } = {}) => {
    if (!shouldShowAccessError(message, { expectedDenied })) {
      setErrorState((prev) => ({ ...prev, page: "" }));
      return;
    }
    setErrorState((prev) => ({ ...prev, page: formatAccessError(message) }));
  };

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

  const handleNavigate = (pageKey) => {
    if (isPageAllowed(pageKey)) {
      setActivePage(pageKey);
    }
  };

  useEffect(() => {
    if (!isAuthenticated || authLoading) {
      return;
    }
    const allowedKeys = visiblePages.map((page) => page.key);
    if (!allowedKeys.length) {
      return;
    }
    if (!allowedKeys.includes(activePage) && !isPageAllowed(activePage)) {
      setActivePage(getFirstAllowedPage() || allowedKeys[0]);
    }
  }, [isAuthenticated, authLoading, visiblePages, activePage, getFirstAllowedPage, isPageAllowed]);

  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    if (!allowedClusters.length) {
      if (selectedClusterId) {
        setSelectedClusterId("");
      }
      return;
    }
    if (!allowedClusters.some((cluster) => cluster.id === selectedClusterId)) {
      setSelectedClusterId(allowedClusters[0].id);
    }
  }, [isAuthenticated, allowedClusters, selectedClusterId]);

  useEffect(() => {
    if (!selectedClusterId) {
      return;
    }
    if (!allowedNamespaces.length) {
      if (selectedNamespace) {
        setSelectedNamespace("");
      }
      return;
    }
    if (!allowedNamespaces.some((ns) => ns.name === selectedNamespace)) {
      setSelectedNamespace(allowedNamespaces[0]?.name || "");
    }
  }, [selectedClusterId, allowedNamespaces, selectedNamespace]);

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
      settings: normalizeSettings({ ...settingsRes, defaultCluster: firstCluster }),
    }));
  };

  // Pick initial cluster only from clusters the user can actually reach (API list ∩ RBAC).
  // Do not restore legacy profile IDs like prod-us-east when the live cluster is docker-desktop.
  useEffect(() => {
    if (!isAuthenticated || !authUser || selectedClusterId || loadingState.core) {
      return;
    }
    if (!allowedClusters.length) {
      return;
    }
    const preferred = settingsDraft?.defaultCluster;
    setSelectedClusterId(resolveDefaultClusterId(allowedClusters, preferred));
  }, [
    isAuthenticated,
    authUser,
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
        const normalizedSettings = normalizeSettings({
          ...settingsRes,
          defaultCluster: firstCluster,
        });
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

  useEffect(() => {
    if (!isAuthenticated || !selectedClusterId) {
      clusterContextClusterRef.current = "";
      return undefined;
    }

    if (resolvedActivePage === "dashboard") {
      return undefined;
    }

    if (!pageNeedsClusterContext(resolvedActivePage)) {
      return undefined;
    }

    const contextReady = clusterContextClusterRef.current === selectedClusterId;

    if (contextReady) {
      if (resolvedActivePage === "clusterOverview") {
        let cancelled = false;
        const loadOverview = async () => {
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
        };
        loadOverview();
        return () => {
          cancelled = true;
        };
      }
      return undefined;
    }

    let cancelled = false;
    const loadClusterContext = async () => {
      const clusterChanged = clusterContextClusterRef.current !== selectedClusterId;
      if (clusterChanged) {
        resourceCache.clearAll();
        setData((prev) => ({
          ...prev,
          namespaces: [],
          resources: emptyNamespaceResources(),
        }));
      }

      setLoadingState((prev) => ({ ...prev, namespaces: true, resources: false }));
      setErrorState((prev) => ({ ...prev, page: "" }));

      let defaultNamespace = "";
      let namespaces = [];
      try {
        const needsOverview = resolvedActivePage === "clusterOverview";
        const [namespacesResult, overviewResult] = await Promise.allSettled([
          listNamespacesByCluster(selectedClusterId),
          needsOverview ? getClusterOverview(selectedClusterId) : Promise.resolve(null),
        ]);

        if (cancelled) {
          return;
        }

        if (namespacesResult.status === "rejected") {
          throw namespacesResult.reason;
        }

        if (overviewResult.status === "fulfilled" && overviewResult.value) {
          setClusterOverview(overviewResult.value);
        } else if (overviewResult.status === "rejected" && needsOverview) {
          applyPageError(
            overviewResult.reason?.message || "Failed to load cluster overview.",
            { expectedDenied: !canAccessCluster(selectedClusterId) }
          );
        }

        namespaces = namespacesResult.value.items || [];
        defaultNamespace = namespaces[0]?.name || "";
        setData((prev) => ({ ...prev, namespaces }));
        clusterContextClusterRef.current = selectedClusterId;
      } catch (loadError) {
        if (!cancelled) {
          clusterContextClusterRef.current = "";
          applyPageError(loadError.message, {
            expectedDenied: !canAccessCluster(selectedClusterId),
          });
          setData((prev) => ({
            ...prev,
            namespaces: [],
            resources: emptyNamespaceResources(),
          }));
        }
        return;
      } finally {
        if (!cancelled) {
          setLoadingState((prev) => ({ ...prev, namespaces: false, resources: false }));
        }
      }

      if (cancelled) {
        return;
      }

      const namespaceStillValid =
        selectedNamespace && namespaces.some((ns) => ns.name === selectedNamespace);
      const activeNamespace = namespaceStillValid ? selectedNamespace : defaultNamespace;

      if (!namespaceStillValid) {
        setSelectedNamespace(activeNamespace);
      }
    };

    loadClusterContext();
    return () => {
      cancelled = true;
      setLoadingState((prev) => ({ ...prev, namespaces: false, resources: false }));
    };
  }, [selectedClusterId, resolvedActivePage, isAuthenticated]);

  useEffect(() => {
    if (!selectedClusterId) {
      setData((prev) => ({ ...prev, alerts: [], alertsMeta: {} }));
      return;
    }

    if (resolvedActivePage === "dashboard") {
      return;
    }

    if (!hasPermission("alerts:view") || !canAccessCluster(selectedClusterId)) {
      setData((prev) => ({ ...prev, alerts: [], alertsMeta: {} }));
      return;
    }

    let cancelled = false;
    const loadAlerts = async () => {
      try {
        const alertsRes = await listAlerts({ cluster: selectedClusterId });
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
    resolvedActivePage,
  ]);

  const requestSignature = (req) => `${req.id}:${req.decidedAt || ""}`;
  const seenRequestStorageKey = authUser?.id
    ? `kubesight.seenRequestUpdates.${authUser.id}`
    : null;
  const dismissedRequestStorageKey = authUser?.id
    ? `kubesight.dismissedRequestUpdates.${authUser.id}`
    : null;

  // Load which decisions this user has already seen so the badge starts correct.
  useEffect(() => {
    if (!seenRequestStorageKey) {
      setSeenRequestSignatures(new Set());
      return;
    }
    try {
      const raw = window.localStorage.getItem(seenRequestStorageKey);
      setSeenRequestSignatures(new Set(raw ? JSON.parse(raw) : []));
    } catch {
      setSeenRequestSignatures(new Set());
    }
  }, [seenRequestStorageKey]);

  // Load decisions the user explicitly cleared so they stay hidden in the bell.
  useEffect(() => {
    if (!dismissedRequestStorageKey) {
      setDismissedRequestSignatures(new Set());
      return;
    }
    try {
      const raw = window.localStorage.getItem(dismissedRequestStorageKey);
      setDismissedRequestSignatures(new Set(raw ? JSON.parse(raw) : []));
    } catch {
      setDismissedRequestSignatures(new Set());
    }
  }, [dismissedRequestStorageKey]);

  // Poll the current user's own requests and surface approved/declined ones in
  // the notifications bell. Independent of the active cluster/page.
  useEffect(() => {
    if (!isAuthenticated || !hasPermission("deployment_requests:request")) {
      setRequestUpdates([]);
      return undefined;
    }
    let cancelled = false;
    const loadRequestUpdates = async () => {
      try {
        const res = await listMyDeploymentRequests({ limit: 100 });
        if (cancelled) {
          return;
        }
        const decided = (res.items || [])
          .filter((row) => row.status && row.status !== "pending")
          .sort(
            (a, b) =>
              new Date(b.decidedAt || b.createdAt) - new Date(a.decidedAt || a.createdAt)
          );
        setRequestUpdates(decided);
      } catch {
        if (!cancelled) {
          setRequestUpdates([]);
        }
      }
    };
    loadRequestUpdates();
    const timer = window.setInterval(loadRequestUpdates, 30000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [isAuthenticated, authUser?.id, hasPermission]);

  const visibleRequestUpdates = useMemo(
    () =>
      requestUpdates.filter(
        (req) => !dismissedRequestSignatures.has(requestSignature(req))
      ),
    [requestUpdates, dismissedRequestSignatures]
  );

  const newRequestCount = useMemo(
    () =>
      visibleRequestUpdates.filter(
        (req) => !seenRequestSignatures.has(requestSignature(req))
      ).length,
    [visibleRequestUpdates, seenRequestSignatures]
  );

  const markRequestUpdatesSeen = () => {
    if (!seenRequestStorageKey || visibleRequestUpdates.length === 0) {
      return;
    }
    setSeenRequestSignatures((prev) => {
      const next = new Set(prev);
      visibleRequestUpdates.forEach((req) => next.add(requestSignature(req)));
      try {
        window.localStorage.setItem(seenRequestStorageKey, JSON.stringify(Array.from(next)));
      } catch {
        // Ignore storage failures (e.g. private mode); badge will simply reappear.
      }
      return next;
    });
  };

  const dismissRequestUpdate = (req) => {
    if (!dismissedRequestStorageKey || !req) {
      return;
    }
    setDismissedRequestSignatures((prev) => {
      const next = new Set(prev);
      next.add(requestSignature(req));
      try {
        window.localStorage.setItem(dismissedRequestStorageKey, JSON.stringify(Array.from(next)));
      } catch {
        // Ignore storage failures; the item will simply reappear next load.
      }
      return next;
    });
  };

  const clearRequestUpdates = () => {
    if (!dismissedRequestStorageKey || visibleRequestUpdates.length === 0) {
      return;
    }
    setDismissedRequestSignatures((prev) => {
      const next = new Set(prev);
      visibleRequestUpdates.forEach((req) => next.add(requestSignature(req)));
      try {
        window.localStorage.setItem(dismissedRequestStorageKey, JSON.stringify(Array.from(next)));
      } catch {
        // Ignore storage failures; items will simply reappear next load.
      }
      return next;
    });
  };

  const normalizeUpgradePayload = (payload) => ({
    clusterInfo: payload.clusterInfo,
    provider: payload.provider,
    versionInfo: payload.versionInfo,
    versionSkew: payload.versionSkew,
    upgradePlan: payload.upgradePlan,
    instructions: payload.instructions || payload.provider?.instructions,
    currentVersion: payload.currentVersion || payload.clusterInfo?.controlPlaneVersion,
    canUpgrade: payload.canUpgrade,
    status: payload.status,
    message: payload.message,
    upgradeId: payload.upgradeId,
    executionSupported: payload.executionSupported,
    requiredConfirmation: payload.requiredConfirmation,
    upgradeChecks: (payload.checks || []).map((check) => ({
      item: check.name,
      state: mapPrecheckState(check.status),
      rawStatus: check.status,
      message: check.details || check.message,
    })),
    upgradeSteps: payload.steps || payload.upgradePlan?.steps || [],
    activeStep: payload.activeStep ?? -1,
  });

  const loadUpgradeInfo = async (clusterId, version = targetVersion, { resetTarget = false } = {}) => {
    if (!clusterId || !canAccessCluster(clusterId)) {
      return;
    }
    upgradeLoadClusterRef.current = clusterId;
    const requestClusterId = clusterId;
    setLoadingState((prev) => ({ ...prev, page: true }));
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const result = await getUpgradeInfo(clusterId, version);
      if (upgradeLoadClusterRef.current !== requestClusterId) {
        return;
      }
      setUpgradeResult({
        ...normalizeUpgradePayload(result),
        clusterId: requestClusterId,
      });
      if (resetTarget) {
        const recommended = result.versionInfo?.recommendedTarget;
        const latest = result.versionInfo?.latestAvailable;
        if (recommended) {
          setTargetVersion(recommended);
        } else if (latest && latest !== "unknown") {
          setTargetVersion(latest);
        } else {
          setTargetVersion(version);
        }
      }
    } catch (infoError) {
      if (upgradeLoadClusterRef.current === requestClusterId) {
        applyPageError(infoError.message);
      }
    } finally {
      if (upgradeLoadClusterRef.current === requestClusterId) {
        setLoadingState((prev) => ({ ...prev, page: false }));
      }
    }
  };

  useEffect(() => {
    if (activePage !== "upgrade" || !selectedClusterId) {
      return;
    }
    setUpgradeResult(null);
    setTargetVersion("v1.31.0");
    loadUpgradeInfo(selectedClusterId, "v1.31.0", { resetTarget: true });
  }, [activePage, selectedClusterId]);

  useEffect(() => {
    const jobId = upgradeResult?.upgradeId || upgradeResult?.jobId;
    if (activePage !== "upgrade" || upgradeResult?.status !== "running" || !jobId) {
      return undefined;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const job = await getUpgradeJob(jobId);
        if (cancelled) {
          return;
        }
        setUpgradeResult((prev) => ({
          ...prev,
          status: job.status,
          message: job.message,
          error: job.error,
          upgradeId: job.jobId || prev?.upgradeId,
          upgradeSteps: job.steps || prev?.upgradeSteps,
          activeStep: job.activeStep ?? prev?.activeStep ?? -1,
          executionSupported: job.executionSupported ?? prev?.executionSupported,
        }));
      } catch {
        // Keep polling until the job completes or the user leaves the page.
      }
    };
    poll();
    const timer = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [activePage, upgradeResult?.status, upgradeResult?.upgradeId, upgradeResult?.jobId]);

  const loadDashboardSummary = async (clusterId, { background = false } = {}) => {
    if (!clusterId || !hasPermission("overview:view") || !canAccessCluster(clusterId)) {
      dashboardLoadClusterRef.current = "";
      setDashboardSummary(null);
      setDashboardRefreshedAt(null);
      setDashboardRefreshing(false);
      setLoadingState((prev) => ({ ...prev, page: false }));
      return;
    }
    const requestSeq = ++dashboardRequestSeqRef.current;
    const isLatestRequest = () => requestSeq === dashboardRequestSeqRef.current;
    dashboardLoadClusterRef.current = clusterId;
    if (background) {
      setDashboardRefreshing(true);
    } else {
      setLoadingState((prev) => ({ ...prev, page: true }));
    }
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const summary = await getDashboardSummary(clusterId);
      if (!isLatestRequest() || dashboardLoadClusterRef.current !== clusterId) {
        return;
      }
      setDashboardSummary(summary);
      setDashboardRefreshedAt(new Date().toISOString());
    } catch (dashboardError) {
      if (!isLatestRequest() || dashboardLoadClusterRef.current !== clusterId) {
        return;
      }
      // The cluster no longer exists (e.g. deleted in another tab) — stop polling
      // it by refreshing the list, which drops it and re-selects a valid cluster.
      if (dashboardError.status === 404) {
        dashboardLoadClusterRef.current = "";
        setDashboardSummary(null);
        setDashboardRefreshedAt(null);
        reloadClusters().catch(() => {});
        return;
      }
      applyPageError(dashboardError.message, {
        expectedDenied: !canAccessCluster(clusterId),
      });
    } finally {
      if (!isLatestRequest()) {
        return;
      }
      if (background) {
        setDashboardRefreshing(false);
      } else {
        setLoadingState((prev) => ({ ...prev, page: false }));
      }
    }
  };

  const selectedClusterIsAllowed = useMemo(
    () => Boolean(selectedClusterId && allowedClusters.some((cluster) => cluster.id === selectedClusterId)),
    [selectedClusterId, allowedClusters]
  );

  useEffect(() => {
    if (resolvedActivePage !== "dashboard" || !selectedClusterIsAllowed) {
      return undefined;
    }
    const clusterChanged =
      Boolean(dashboardSummaryClusterRef.current) &&
      dashboardSummaryClusterRef.current !== selectedClusterId;
    if (clusterChanged || !dashboardSummaryClusterRef.current) {
      setDashboardSummary(null);
      setDashboardRefreshedAt(null);
    }
    dashboardSummaryClusterRef.current = selectedClusterId;
    loadDashboardSummary(selectedClusterId);
    const intervalSeconds = Number(settingsDraft.refreshIntervalSeconds) || 30;
    const intervalMs = Math.min(Math.max(intervalSeconds, 30), 60) * 1000;
    const timer = window.setInterval(() => {
      loadDashboardSummary(selectedClusterId, { background: true });
    }, intervalMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [resolvedActivePage, selectedClusterId, selectedClusterIsAllowed, settingsDraft.refreshIntervalSeconds]);

  const loadInventory = async () => {
    if (!hasPermission("inventory:view") && !hasPermission("resources:view")) {
      setInventoryItems([]);
      return;
    }
    setLoadingState((prev) => ({ ...prev, page: true }));
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const items = await listInventory(
        selectedClusterId ? { cluster: selectedClusterId } : undefined
      );
      setInventoryItems(Array.isArray(items) ? items : []);
    } catch (inventoryError) {
      applyPageError(inventoryError.message);
      setInventoryItems([]);
    } finally {
      setLoadingState((prev) => ({ ...prev, page: false }));
    }
  };

  const loadApplicationDetail = async (inventoryId) => {
    if (!inventoryId) {
      setApplicationDetail(null);
      return;
    }
    const requestId = ++applicationDetailRequestRef.current;
    setApplicationDetail(null);
    setLoadingState((prev) => ({ ...prev, page: true }));
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const detail = await getInventoryDetail(inventoryId);
      if (requestId !== applicationDetailRequestRef.current) {
        return;
      }
      setApplicationDetail(detail);
    } catch (detailError) {
      if (requestId !== applicationDetailRequestRef.current) {
        return;
      }
      applyPageError(detailError.message);
      setApplicationDetail(null);
    } finally {
      if (requestId === applicationDetailRequestRef.current) {
        setLoadingState((prev) => ({ ...prev, page: false }));
      }
    }
  };

  useEffect(() => {
    if (activePage !== "inventory") {
      return undefined;
    }
    loadInventory();
    return undefined;
  }, [activePage, selectedClusterId]);

  useEffect(() => {
    if (activePage !== "applicationDetails" || !selectedApplicationId) {
      return;
    }
    loadApplicationDetail(selectedApplicationId);
  }, [activePage, selectedApplicationId]);

  const handleSelectApplication = (inventoryId, tab = "overview") => {
    if (!inventoryId) {
      return;
    }
    setSelectedApplicationId(inventoryId);
    setApplicationDetailsTab(tab);
    if (isPageAllowed("applicationDetails")) {
      setActivePage("applicationDetails");
    }
  };

  const inventoryClusterOptions = useMemo(() => {
    const byId = new Map();
    allowedClusters.forEach((cluster) => {
      byId.set(cluster.id, { id: cluster.id, name: cluster.name || cluster.id });
    });
    inventoryItems.forEach((item) => {
      const id = item.cluster || item.clusterId;
      if (id && !byId.has(id)) {
        byId.set(id, { id, name: id });
      }
    });
    return [...byId.values()].sort((a, b) =>
      String(a.name).localeCompare(String(b.name))
    );
  }, [inventoryItems, allowedClusters]);

  const inventoryNamespaceOptions = useMemo(() => {
    const ns = new Set(inventoryItems.map((item) => item.namespace).filter(Boolean));
    return [...ns].sort();
  }, [inventoryItems]);

  const handleRemoveFromInventory = async (detail) => {
    const entryId = detail?.summary?.catalogEntryId || detail?.catalog?.id;
    if (!entryId) return;
    const confirmed = window.confirm(
      "This removes the app from KubeSight inventory metadata only.\nIt will not delete anything from the Kubernetes cluster."
    );
    if (!confirmed) return;
    try {
      await removeFromInventory(entryId);
      handleNavigate("inventory");
      loadInventory();
    } catch (err) {
      applyPageError(err.message);
    }
  };

  const handleSaveCatalogEdit = async (payload) => {
    const entryId = applicationDetail?.summary?.catalogEntryId || applicationDetail?.catalog?.id;
    if (!entryId) return;
    setEditCatalogSaving(true);
    setEditCatalogError("");
    try {
      await updateCatalogEntry(entryId, payload);
      setEditCatalogOpen(false);
      await loadApplicationDetail(selectedApplicationId);
      loadInventory();
    } catch (err) {
      setEditCatalogError(err.message || "Update failed");
    } finally {
      setEditCatalogSaving(false);
    }
  };

  const runPrecheck = async () => {
    if (!selectedClusterId || !canAccessCluster(selectedClusterId)) {
      return;
    }
    setLoadingState((prev) => ({ ...prev, page: true }));
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const result = await runUpgradePrecheck({
        clusterId: selectedClusterId,
        targetVersion,
      });
      setUpgradeResult({
        ...normalizeUpgradePayload(result),
        clusterId: selectedClusterId,
        canUpgrade: Boolean(result.canUpgrade),
        status: result.canUpgrade ? null : "blocked",
        message: result.canUpgrade
          ? `Precheck passed. Current version: ${result.currentVersion || "unknown"}`
          : "Precheck failed. Fix failed checks before upgrading.",
      });
    } catch (precheckError) {
      applyPageError(precheckError.message);
    } finally {
      setLoadingState((prev) => ({ ...prev, page: false }));
    }
  };

  const runUpgradeStart = async () => {
    if (!selectedClusterId || !canAccessCluster(selectedClusterId)) {
      return;
    }
    const showsInstructionsOnly =
      upgradeResult?.provider?.executionMode === "instructions" ||
      (!upgradeResult?.provider?.upgradeSupported &&
        upgradeResult?.provider?.executionMode !== "plan-only" &&
        upgradeResult?.provider?.executionMode !== "execute-with-cli");

    if (showsInstructionsOnly) {
      document.querySelector(".upgrade-instructions")?.scrollIntoView({ behavior: "smooth" });
      return;
    }

    if (upgradeResult && upgradeResult.canUpgrade === false) {
      setErrorState((prev) => ({ ...prev, page: "Run a successful precheck before starting upgrade." }));
      return;
    }

    const willRunAutomatically = upgradeResult?.provider?.executionMode === "execute-with-cli";
    if (willRunAutomatically) {
      const clusterLabel = allowedClusters.find((c) => c.id === selectedClusterId)?.name || selectedClusterId;
      const confirmed = window.confirm(
        `Start automatic upgrade of cluster "${clusterLabel}" to ${targetVersion}?\n\nKubeSight will drain nodes, run kubeadm upgrade steps, and restart kubelet on each node.\n\nThis operation modifies your cluster. Continue?`
      );
      if (!confirmed) return;
    }

    setLoadingState((prev) => ({ ...prev, page: true }));
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const result = await startUpgrade({
        clusterId: selectedClusterId,
        targetVersion,
      });
      const normalized = normalizeUpgradePayload(result);
      const completedIndex = (normalized.upgradeSteps || []).reduce(
        (last, step, index) => (step.status === "completed" ? index : last),
        -1
      );
      setUpgradeResult({
        ...normalized,
        clusterId: selectedClusterId,
        canUpgrade: upgradeResult?.canUpgrade ?? true,
        status: result.status,
        message: result.message,
        activeStep: result.activeStep ?? completedIndex,
        upgradeSteps: result.steps || normalized.upgradeSteps,
        executionSupported: result.executionSupported ?? normalized.executionSupported,
        jobId: result.jobId || result.upgradeId,
      });
      requestAnimationFrame(() => {
        if (result.status === "manual_required") {
          document.querySelector(".upgrade-result-banner")?.scrollIntoView({
            behavior: "smooth",
            block: "start",
          });
          return;
        }
        document.querySelector(".upgrade-plan-result")?.scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
      });
    } catch (startError) {
      applyPageError(startError.message);
    } finally {
      setLoadingState((prev) => ({ ...prev, page: false }));
    }
  };

  const handleTargetVersionChange = (version) => {
    setTargetVersion(version);
    if (selectedClusterId && activePage === "upgrade") {
      loadUpgradeInfo(selectedClusterId, version);
    }
  };

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
      const refreshedSettings = normalizeSettings(await getSettings());
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
      const refreshedSettings = normalizeSettings(await getSettings());
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
            summary={dashboardSummary}
            loading={loadingState.page}
            refreshing={dashboardRefreshing}
            coreLoading={loadingState.core}
            accessError={errorState.page}
            hasClusters={hasClusters}
            selectedCluster={selectedCluster}
            onRefresh={() =>
              loadDashboardSummary(selectedClusterId, {
                background: Boolean(dashboardSummary?.clusterId === selectedClusterId),
              })
            }
            lastRefreshedAt={dashboardRefreshedAt}
            onNavigateToUpgrade={() => handleNavigate("upgrade")}
            onNavigateToInventory={() => handleNavigate("inventory")}
            canOpenUpgrade={isPageAllowed("upgrade")}
            canOpenInventory={isPageAllowed("inventory")}
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
            clusterOptions={inventoryClusterOptions}
            defaultClusterId={selectedClusterId}
            canRegister={hasPermission("inventory:register")}
            canDeploy={hasPermission("apps:deploy")}
            canHelmInstall={hasPermission("helm:install")}
            canManageTemplates={hasPermission("inventory:update")}
            onRefresh={loadInventory}
          />
        );
      case "applicationDetails":
        return (
          <>
            <ApplicationDetailsPage
              detail={applicationDetail}
              selectedApplicationId={selectedApplicationId}
              loading={loadingState.page}
              user={authUser}
              activeTab={applicationDetailsTab}
              onTabChange={setApplicationDetailsTab}
              onBack={() => handleNavigate("inventory")}
              onRefreshDetail={() => loadApplicationDetail(selectedApplicationId)}
              canViewLogs={hasPermission("logs:view")}
              canUpdateCatalog={hasPermission("inventory:update")}
              canRemoveFromInventory={hasPermission("inventory:remove")}
              canDeploy={hasPermission("apps:deploy")}
              canViewHelm={hasPermission("helm:view")}
              canUpgradeHelm={hasPermission("helm:upgrade")}
              canRollbackHelm={hasPermission("helm:rollback")}
              canUninstallHelm={hasPermission("helm:uninstall")}
              onEditCatalog={() => setEditCatalogOpen(true)}
              onRemoveFromInventory={handleRemoveFromInventory}
              onDeployUpdate={() => {}}
              clusterOptions={inventoryClusterOptions}
              onHelmUpgrade={() => handleNavigate("inventory")}
              onHelmActionComplete={() => {
                loadApplicationDetail(selectedApplicationId);
                loadInventory();
              }}
            />
            {editCatalogOpen ? (
              <Suspense fallback={null}>
                <EditCatalogModal
                  open={editCatalogOpen}
                  catalog={applicationDetail?.catalog || {}}
                  onClose={() => setEditCatalogOpen(false)}
                  onSave={handleSaveCatalogEdit}
                  saving={editCatalogSaving}
                  error={editCatalogError}
                />
              </Suspense>
            ) : null}
          </>
        );
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
          <ResourcesPage
            data={scopedData}
            rawResources={cachedRawResources}
            clusterId={selectedClusterId}
            namespace={selectedNamespace}
            hasClusters={hasClusters}
            hasNamespaces={hasNamespaces}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            activeTab={resourceActiveTab}
            onActiveTabChange={setResourceActiveTab}
            onRefreshTab={() => refreshResourceTab(listKeyForTab(resourceActiveTab))}
            tabLoading={isTabLoading(listKeyForTab(resourceActiveTab))}
            tabRefreshing={isTabRefreshing(listKeyForTab(resourceActiveTab))}
            isTabLoaded={isTabLoaded}
            tabErrors={resourceTabErrors}
            accessError={pageAccessError}
            visibleTabs={visibleResourceTabs}
            isAdmin={isAdmin}
            onNavigateToLogs={(prefill) => {
              if (prefill?.clusterId) {
                setSelectedClusterId(prefill.clusterId);
              }
              if (prefill?.namespace) {
                setSelectedNamespace(prefill.namespace);
              }
              setPreferredLogPod(prefill?.pod || "");
              handleNavigate("logs");
            }}
          />
        );
      case "logs":
        return (
          <LogsPage
            clusters={allowedClusters}
            namespaces={allowedNamespaces}
            selectedClusterId={selectedClusterId}
            selectedNamespace={selectedNamespace}
            preferredPod={preferredLogPod}
            onPreferredPodApplied={() => setPreferredLogPod("")}
            onClusterChange={setSelectedClusterId}
            onNamespaceChange={setSelectedNamespace}
            hasClusters={hasClusters}
            hasNamespaces={hasNamespaces}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
      case "alertPolicies":
        return (
          <AlertPoliciesPage
            clusterId={selectedClusterId}
            clusterOptions={allowedClusters}
            selectedNamespace={selectedNamespace}
            allowedNamespaces={allowedNamespaces}
            hasClusters={hasClusters}
            canManage={hasPermission("alerts:manage")}
            coreLoading={loadingState.core}
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
            allowedResources={allowedResources}
            canManageRouting={isAdmin}
            onNavigateToAlertRouting={() => handleNavigate("alertRouting")}
            canManageAlerts={hasPermission("alerts:manage")}
            hasClusters={hasClusters}
            authUser={authUser}
            onNavigateToAlertPolicies={() => handleNavigate("alertPolicies")}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
      case "alertRouting":
        return <AlertRoutingPage />;
      case "upgrade":
        return (
          <UpgradeSafeModePage
            upgradeData={upgradeResult}
            targetVersion={targetVersion}
            onTargetVersionChange={handleTargetVersionChange}
            onRunPrecheck={runPrecheck}
            onStartUpgrade={runUpgradeStart}
            onViewInstructions={() =>
              document.querySelector(".upgrade-instructions")?.scrollIntoView({ behavior: "smooth" })
            }
            loading={loadingState.page}
            coreLoading={loadingState.core}
            hasClusters={hasClusters}
            accessError={pageAccessError}
            canPrecheck={hasPermission("upgrades:precheck") && canAccessCluster(selectedClusterId)}
            canStart={hasPermission("upgrades:start") && canAccessCluster(selectedClusterId)}
          />
        );
      case "applicationServices":
        return <ApplicationServicesPage clusters={allowedClusters} />;
      case "clients":
        return <ClientsPage />;
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
      case "settings":
        return (
          <SettingsPage
            data={{ ...data, user: displayUser }}
            clusters={allowedClusters}
            settingsDraft={settingsDraft}
            onSettingsChange={handleSettingsDraftChange}
            onSave={saveSettings}
            saving={loadingState.page}
            canManage={hasPermission("settings:manage")}
            canManageAlertRouting={isAdmin}
            onNavigateToAlertRouting={() => handleNavigate("alertRouting")}
          />
        );
      default:
        return (
          <DashboardPage
            summary={dashboardSummary}
            loading={loadingState.page}
            refreshing={dashboardRefreshing}
            coreLoading={loadingState.core}
            accessError={errorState.page}
            hasClusters={hasClusters}
            selectedCluster={selectedCluster}
            onRefresh={() =>
              loadDashboardSummary(selectedClusterId, {
                background: Boolean(dashboardSummary?.clusterId === selectedClusterId),
              })
            }
            lastRefreshedAt={dashboardRefreshedAt}
            onNavigateToUpgrade={() => handleNavigate("upgrade")}
            onNavigateToInventory={() => handleNavigate("inventory")}
            canOpenUpgrade={isPageAllowed("upgrade")}
            canOpenInventory={isPageAllowed("inventory")}
          />
        );
    }
  };

  let pageNode = null;
  if (!visiblePages.length) {
    pageNode = (
      <Suspense fallback={<RouteLoadingFallback label="Loading..." />}>
        <NoFeaturesPage />
      </Suspense>
    );
  } else if (!resolvedActivePage) {
    pageNode = (
      <Suspense fallback={<RouteLoadingFallback label="Loading..." />}>
        <NoFeaturesPage />
      </Suspense>
    );
  } else {
    pageNode = (
      <Suspense fallback={<RouteLoadingFallback pageKey={resolvedActivePage} />}>
        {renderPage(resolvedActivePage)}
      </Suspense>
    );
  }

  const alertBadgeCount = isDashboardPage
    ? dashboardSummary?.alerts?.total || 0
    : Array.isArray(data.alerts)
      ? data.alerts.length
      : 0;
  const activeClusterLabel =
    allowedClusters.find((cluster) => cluster.id === selectedClusterId)?.name || selectedClusterId || "";

  if (authLoading) {
    return (
      <div className="login-screen">
        <p className="muted">Loading session...</p>
      </div>
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

  const clusterBanner =
    !deferGlobalMessages &&
    isAuthenticated &&
    !hasClusters &&
    activePage !== "userManagement" &&
    activePage !== "auditLogs" &&
    activePage !== "settings" &&
    activePage !== "alertRouting"
      ? EMPTY_MESSAGES.noClusters
      : "";

  const globalErrorMessage = deferGlobalMessages ? "" : errorState.core;

  const showLoadingOverlay = loadingState.core;
  const loadingOverlayLabel = getScopeLoadingLabel({
    coreLoading: loadingState.core,
    namespacesLoading,
    resourcesLoading,
    pageLoading: isDashboardPage ? loadingState.page : false,
  });
  const loadingOverlayHint =
    namespacesLoading || resourcesLoading ? SCOPE_LOADING_HINT : undefined;

  return (
    <>
    <AppShell
      visiblePages={visiblePages}
      activePage={activePage === "applicationDetails" ? "inventory" : activePage}
      onNavigate={handleNavigate}
      allowedClusters={allowedClusters}
      allowedNamespaces={allowedNamespaces}
      selectedClusterId={selectedClusterId}
      selectedNamespace={selectedNamespace}
      onClusterChange={setSelectedClusterId}
      onNamespaceChange={setSelectedNamespace}
      loadingCore={loadingState.core}
      loadingNamespaces={namespacesLoading}
      loadingResources={resourcesLoading}
      loadingPage={scopeDataLoading || loadingState.page}
      loadingOverlay={showLoadingOverlay}
      loadingOverlayLabel={loadingOverlayLabel}
      loadingOverlayHint={loadingOverlayHint}
      errorMessage={globalErrorMessage}
      clusterBannerMessage={clusterBanner}
      showClusterSelector={pageNeedsClusterContext(activePage)}
      showNamespaceSelector={pageNeedsNamespaceContext(activePage)}
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
    >
      {pageNode}
    </AppShell>
    {changeBundle.enabled ? (
      <>
        {!changeBundle.isOpen && activePage !== "resources" ? (
        <button
          type="button"
          aria-label="Open change bundle"
          onClick={changeBundle.openDrawer}
          style={{
            position: "fixed",
            right: 20,
            bottom: 20,
            zIndex: 60,
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 16px",
            borderRadius: 999,
            border: "1px solid #334155",
            background: "#38bdf8",
            color: "#0f172a",
            fontWeight: 600,
            boxShadow: "0 8px 24px rgba(0,0,0,.35)",
            cursor: "pointer",
          }}
        >
          Change Bundle
          {changeBundle.itemCount > 0 ? (
            <span
              style={{
                background: "#0f172a",
                color: "#38bdf8",
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
