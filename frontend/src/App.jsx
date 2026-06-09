import { useEffect, useMemo, useRef, useState } from "react";
import {
  getClusterOverview,
  getDashboardSummary,
  getInventoryDetail,
  listInventory,
  getSettings,
  listAlerts,
  listClusters,
  testAlertEmail,
  listNamespacesByCluster,
  getUpgradeInfo,
  runUpgradePrecheck,
  startUpgrade,
  updateSettings,
} from "./api";
import { useAuth } from "./context/AuthContext";
import AppShell from "./components/layout/AppShell.jsx";
import { NoFeaturesPage } from "./pages/AccessDeniedPage";
import AlertsPage from "./pages/AlertsPage.jsx";
import AuditLogsPage from "./pages/AuditLogsPage";
import ClusterManagementPage from "./pages/ClusterManagementPage.jsx";
import ClusterOverviewPage from "./pages/ClusterOverviewPage.jsx";
import ClustersPage from "./pages/ClustersPage.jsx";
import DashboardPage from "./pages/DashboardPage.jsx";
import LoginPage from "./pages/LoginPage";
import LogsPage from "./pages/LogsPage.jsx";
import NamespacesPage from "./pages/NamespacesPage.jsx";
import InventoryPage from "./pages/InventoryPage.jsx";
import ApplicationDetailsPage from "./pages/ApplicationDetailsPage.jsx";
import EditCatalogModal from "./components/inventory/EditCatalogModal.jsx";
import ResourcesPage from "./pages/ResourcesPage.jsx";
import AlertPoliciesPage from "./pages/AlertPoliciesPage.jsx";
import SettingsPage from "./pages/SettingsPage.jsx";
import UpgradeSafeModePage from "./pages/UpgradeSafeModePage.jsx";
import UserManagementPage from "./pages/UserManagementPage";
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
    getAllowedClusters,
    getAllowedNamespaces,
    getAllowedResources,
    canAccessCluster,
    getVisibleResourceTabs,
    shouldShowAccessError,
    filterAlertsForUser,
    isAdmin,
  } = useAuth();
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
  const applicationDetailRequestRef = useRef(0);
  const clusterContextClusterRef = useRef("");

  const allowedClusters = useMemo(
    () => getAllowedClusters(data.clusters),
    [data.clusters, getAllowedClusters]
  );
  const allowedNamespaces = useMemo(
    () => getAllowedNamespaces(selectedClusterId, data.namespaces),
    [selectedClusterId, data.namespaces, getAllowedNamespaces]
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
    const filtered = getAllowedClusters(clusters);
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

  useEffect(() => {
    if (!isAuthenticated || !authUser || selectedClusterId) {
      return;
    }
    const preferred = settingsDraft?.defaultCluster;
    const userClusterIds = authUser.clusterAccess || [];
    if (!isAdmin && userClusterIds.length) {
      setSelectedClusterId(
        preferred && userClusterIds.includes(preferred) ? preferred : userClusterIds[0]
      );
      return;
    }
    if (preferred) {
      setSelectedClusterId(preferred);
    }
  }, [
    isAuthenticated,
    authUser,
    selectedClusterId,
    settingsDraft.defaultCluster,
    isAdmin,
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

    const clusterChanged = clusterContextClusterRef.current !== selectedClusterId;

    // Same cluster, different page (e.g. resources → logs): keep namespaces/resources.
    if (!clusterChanged) {
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
      clusterContextClusterRef.current = selectedClusterId;
      resourceCache.clearAll();
      setLoadingState((prev) => ({ ...prev, namespaces: true, resources: false }));
      setErrorState((prev) => ({ ...prev, page: "" }));
      setData((prev) => ({
        ...prev,
        namespaces: [],
        resources: emptyNamespaceResources(),
      }));

      let defaultNamespace = "";
      let namespaces = [];
      try {
        const namespacesRes = await listNamespacesByCluster(selectedClusterId);
        if (cancelled) {
          return;
        }

        if (resolvedActivePage === "clusterOverview") {
          const overview = await getClusterOverview(selectedClusterId);
          if (cancelled) {
            return;
          }
          setClusterOverview(overview);
        }

        namespaces = getAllowedNamespaces(
          selectedClusterId,
          namespacesRes.items || []
        );
        defaultNamespace = namespaces[0]?.name || "";
        setData((prev) => ({ ...prev, namespaces }));
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
          setLoadingState((prev) => ({ ...prev, namespaces: false, resources: false }));
        }
        return;
      }

      if (cancelled) {
        return;
      }

      const namespaceStillValid =
        selectedNamespace && namespaces.some((ns) => ns.name === selectedNamespace);
      const activeNamespace = namespaceStillValid ? selectedNamespace : defaultNamespace;

      setLoadingState((prev) => ({
        ...prev,
        namespaces: false,
        resources: false,
      }));
      if (!namespaceStillValid) {
        setSelectedNamespace(activeNamespace);
      }
    };

    loadClusterContext();
    return () => {
      cancelled = true;
    };
  }, [selectedClusterId, resolvedActivePage]);

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

  const loadUpgradeInfo = async (clusterId, version = targetVersion) => {
    if (!clusterId || !canAccessCluster(clusterId)) {
      return;
    }
    try {
      const result = await getUpgradeInfo(clusterId, version);
      setUpgradeResult((prev) => ({
        ...normalizeUpgradePayload(result),
        upgradeChecks: prev?.upgradeChecks?.length ? prev.upgradeChecks : [],
        canUpgrade: prev?.canUpgrade,
        status: prev?.status,
        message: prev?.message,
      }));
      if (result.versionInfo?.latestAvailable && result.versionInfo.latestAvailable !== "unknown") {
        setTargetVersion((current) =>
          current === "v1.31.0" ? result.versionInfo.latestAvailable : current
        );
      }
    } catch (infoError) {
      applyPageError(infoError.message);
    }
  };

  useEffect(() => {
    if (activePage !== "upgrade" || !selectedClusterId) {
      return;
    }
    loadUpgradeInfo(selectedClusterId, targetVersion);
  }, [activePage, selectedClusterId]);

  const loadDashboardSummary = async (clusterId) => {
    if (!clusterId || !hasPermission("overview:view")) {
      setDashboardSummary(null);
      setLoadingState((prev) => ({ ...prev, page: false }));
      return;
    }
    setLoadingState((prev) => ({ ...prev, page: true }));
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const summary = await getDashboardSummary(clusterId);
      setDashboardSummary(summary);
      setDashboardRefreshedAt(new Date().toISOString());
    } catch (dashboardError) {
      applyPageError(dashboardError.message, {
        expectedDenied: !canAccessCluster(clusterId),
      });
    } finally {
      setLoadingState((prev) => ({ ...prev, page: false }));
    }
  };

  useEffect(() => {
    if (resolvedActivePage !== "dashboard" || !selectedClusterId) {
      return undefined;
    }
    loadDashboardSummary(selectedClusterId);
    const intervalSeconds = Number(settingsDraft.refreshIntervalSeconds) || 30;
    const intervalMs = Math.min(Math.max(intervalSeconds, 30), 60) * 1000;
    const timer = window.setInterval(() => {
      loadDashboardSummary(selectedClusterId);
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [resolvedActivePage, selectedClusterId, settingsDraft.refreshIntervalSeconds]);

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
        canUpgrade: Boolean(result.canUpgrade),
        status: result.canUpgrade ? "ready" : "blocked",
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
        upgradeResult?.provider?.executionMode !== "plan-only");

    if (showsInstructionsOnly) {
      document.querySelector(".upgrade-instructions")?.scrollIntoView({ behavior: "smooth" });
      return;
    }

    if (upgradeResult && upgradeResult.canUpgrade === false) {
      setErrorState((prev) => ({ ...prev, page: "Run a successful precheck before starting upgrade." }));
      return;
    }

    setLoadingState((prev) => ({ ...prev, page: true }));
    setErrorState((prev) => ({ ...prev, page: "" }));
    try {
      const result = await startUpgrade({
        clusterId: selectedClusterId,
        targetVersion,
        confirmation: confirmationText || undefined,
      });
      const normalized = normalizeUpgradePayload(result);
      const completedIndex = (normalized.upgradeSteps || []).reduce(
        (last, step, index) => (step.status === "completed" ? index : last),
        -1
      );
      setUpgradeResult({
        ...normalized,
        canUpgrade: upgradeResult?.canUpgrade ?? true,
        status: result.status,
        message: result.message,
        activeStep: result.activeStep ?? completedIndex,
        requiredConfirmation: result.requiredConfirmation,
      });
      if (result.status === "confirmation_required") {
        setConfirmationText("");
      }
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
            coreLoading={loadingState.core}
            accessError={errorState.page}
            hasClusters={hasClusters}
            selectedCluster={selectedCluster}
            onRefresh={() => loadDashboardSummary(selectedClusterId)}
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
            <EditCatalogModal
              open={editCatalogOpen}
              catalog={applicationDetail?.catalog || {}}
              onClose={() => setEditCatalogOpen(false)}
              onSave={handleSaveCatalogEdit}
              saving={editCatalogSaving}
              error={editCatalogError}
            />
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
            settings={data.settings}
            onSaveAlertRouting={saveAlertRouting}
            onTestAlertEmail={testAlertEmailDelivery}
            savingRouting={savingRouting}
            routingError={routingError}
            testingEmail={testingEmail}
            testEmailMessage={testEmailMessage}
            canManageAlerts={hasPermission("alerts:manage")}
            hasClusters={hasClusters}
            authUser={authUser}
            onNavigateToAlertPolicies={() => handleNavigate("alertPolicies")}
            coreLoading={loadingState.core}
            namespacesLoading={namespacesLoading}
            accessError={pageAccessError}
          />
        );
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
            showConfirmation={upgradeResult?.status === "confirmation_required"}
            confirmationText={confirmationText}
            onConfirmationChange={setConfirmationText}
            requiredConfirmation={upgradeResult?.requiredConfirmation}
          />
        );
      case "userManagement":
        return <UserManagementPage />;
      case "auditLogs":
        return <AuditLogsPage />;
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
          />
        );
      default:
        return (
          <DashboardPage
            summary={dashboardSummary}
            loading={loadingState.page}
            coreLoading={loadingState.core}
            accessError={errorState.page}
            hasClusters={hasClusters}
            selectedCluster={selectedCluster}
            onRefresh={() => loadDashboardSummary(selectedClusterId)}
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
    pageNode = <NoFeaturesPage />;
  } else if (!resolvedActivePage) {
    pageNode = <NoFeaturesPage />;
  } else {
    pageNode = renderPage(resolvedActivePage);
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
    return <LoginPage />;
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
    activePage !== "settings"
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
      displayUser={displayUser}
      userInitials={userInitials}
      onLogout={logout}
    >
      {pageNode}
    </AppShell>
  );
}
