import { useEffect, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import ResourcesPage from "./ResourcesPage.jsx";
import { useAuth } from "../context/AuthContext.jsx";
import { useNamespaceResourceCache } from "../hooks/useNamespaceResourceCache.js";
import { emptyNamespaceResources, listKeyForTab } from "../lib/resourceTypes.js";

/**
 * Route container for the workloads of one namespace.
 *
 * Audit items M2 and M3. The cache was enabled from App by asking
 * `pageNeedsResourceData(activePage)` — a `Set` of page keys kept in
 * `accessViewState.js`, a third file that had to be updated whenever a page
 * started or stopped needing resource data. Mounting is that condition, stated
 * once, in the only place that can be wrong about it.
 *
 * The active tab moves into the URL, so `/workloads/prod-eu/payments?tab=services`
 * is a link. It was App state, which meant the tab an operator was reading
 * could not be sent to anyone and did not survive a refresh.
 */
export default function ResourcesRoute({
  clusterId,
  namespace,
  scopedData,
  hasClusters,
  hasNamespaces,
  coreLoading,
  namespacesLoading,
  accessError,
}) {
  const auth = useAuth();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const visibleTabs = useMemo(
    () => auth.getVisibleResourceTabs(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [auth.getVisibleResourceTabs, auth.user?.id]
  );

  const requestedTab = searchParams.get("tab") || "";
  // A tab this user may not see — from an old link, or a permission change
  // since it was sent — falls back rather than rendering an empty panel.
  const activeTab =
    requestedTab && visibleTabs.includes(requestedTab) ? requestedTab : visibleTabs[0] || "pods";

  const setActiveTab = (next) => {
    const params = new URLSearchParams(searchParams);
    if (next && next !== visibleTabs[0]) {
      params.set("tab", next);
    } else {
      params.delete("tab");
    }
    // replace: flicking through tabs should not fill the back stack.
    setSearchParams(params, { replace: true });
  };

  // Drop a tab from the URL that this user cannot open, so the address matches
  // what is on screen and stays shareable.
  useEffect(() => {
    if (requestedTab && visibleTabs.length && !visibleTabs.includes(requestedTab)) {
      const params = new URLSearchParams(searchParams);
      params.delete("tab");
      setSearchParams(params, { replace: true });
    }
  }, [requestedTab, visibleTabs, searchParams, setSearchParams]);

  const enabled = Boolean(clusterId && namespace);
  const activeListKey = enabled ? listKeyForTab(activeTab) : "";

  const {
    resources: cachedResources,
    rawResources,
    refreshTab,
    isTabLoading,
    isTabRefreshing,
    isTabLoaded,
    tabErrors,
    activeTabLoading,
  } = useNamespaceResourceCache({
    clusterId,
    namespace,
    activeListKey,
    enabled,
    filterResources: auth.getAllowedResources,
  });

  const data = useMemo(
    () => ({
      ...scopedData,
      resources: enabled ? cachedResources : emptyNamespaceResources(),
    }),
    [scopedData, enabled, cachedResources]
  );

  return (
    <ResourcesPage
      data={data}
      rawResources={rawResources}
      clusterId={clusterId}
      namespace={namespace}
      hasClusters={hasClusters}
      hasNamespaces={hasNamespaces}
      coreLoading={coreLoading}
      namespacesLoading={namespacesLoading || activeTabLoading}
      activeTab={activeTab}
      onActiveTabChange={setActiveTab}
      onRefreshTab={() => refreshTab(listKeyForTab(activeTab))}
      tabLoading={isTabLoading(listKeyForTab(activeTab))}
      tabRefreshing={isTabRefreshing(listKeyForTab(activeTab))}
      isTabLoaded={isTabLoaded}
      tabErrors={tabErrors}
      accessError={accessError}
      visibleTabs={visibleTabs}
      isAdmin={auth.isAdmin}
      onNavigateToLogs={(prefill) => {
        // The pod carries in the URL rather than through a write-then-clear
        // handshake in App, so the resulting Logs view is itself a link.
        const params = new URLSearchParams();
        if (prefill?.clusterId) params.set("cluster", prefill.clusterId);
        if (prefill?.namespace) params.set("namespace", prefill.namespace);
        if (prefill?.pod) params.set("pod", prefill.pod);
        navigate(`/logs?${params.toString()}`);
      }}
    />
  );
}
