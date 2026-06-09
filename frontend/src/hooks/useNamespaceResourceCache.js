import { useCallback, useEffect, useRef, useState } from "react";
import { getResourceListByType } from "../api/clustersApi.js";
import { emptyNamespaceResources } from "../lib/resourceTypes.js";
import { resourceCache, RESOURCE_CACHE_TTL_MS } from "../services/resourceCacheService.js";

function setsEqual(a, b) {
  if (a.size !== b.size) {
    return false;
  }
  for (const value of a) {
    if (!b.has(value)) {
      return false;
    }
  }
  return true;
}

/**
 * On-demand per-tab namespace resource fetching with client-side cache.
 *
 * @param {object} options
 * @param {string} options.clusterId
 * @param {string} options.namespace
 * @param {string} options.activeListKey - resource list key to load/refresh (e.g. "pods")
 * @param {boolean} options.enabled - fetch when true
 * @param {function} options.filterResources - RBAC filter: (clusterId, namespace, partial) => filtered
 */
export function useNamespaceResourceCache({
  clusterId,
  namespace,
  activeListKey = "",
  enabled = true,
  filterResources,
}) {
  const [resources, setResources] = useState(emptyNamespaceResources);
  const [rawResources, setRawResources] = useState(emptyNamespaceResources);
  const [loadingKeys, setLoadingKeys] = useState(() => new Set());
  const [refreshingKeys, setRefreshingKeys] = useState(() => new Set());
  const [tabErrors, setTabErrors] = useState({});
  const filterRef = useRef(filterResources);
  const scopeRef = useRef({ clusterId: "", namespace: "" });

  filterRef.current = filterResources;

  const syncFromCache = useCallback((cid, ns) => {
    const bucket = resourceCache.buildResourcesBucket(cid, ns);
    setRawResources(bucket);
    const filter = filterRef.current;
    setResources(filter ? filter(cid, ns, { namespace: ns, ...bucket }) : bucket);
  }, []);

  const setKeyLoading = useCallback((listKey, isLoading) => {
    setLoadingKeys((prev) => {
      const next = new Set(prev);
      if (isLoading) {
        next.add(listKey);
      } else {
        next.delete(listKey);
      }
      return setsEqual(prev, next) ? prev : next;
    });
  }, []);

  const setKeyRefreshing = useCallback((listKey, isRefreshing) => {
    setRefreshingKeys((prev) => {
      const next = new Set(prev);
      if (isRefreshing) {
        next.add(listKey);
      } else {
        next.delete(listKey);
      }
      return setsEqual(prev, next) ? prev : next;
    });
  }, []);

  const applyListKey = useCallback(
    (cid, ns, listKey, items) => {
      resourceCache.set(cid, ns, listKey, items);
      syncFromCache(cid, ns);
    },
    [syncFromCache]
  );

  const fetchListKey = useCallback(
    async (listKey, { force = false, silent = false } = {}) => {
      if (!clusterId || !namespace || !listKey) {
        return;
      }

      const cached = resourceCache.get(clusterId, namespace, listKey);
      const isStale = resourceCache.isStale(cached);

      if (!force && cached && !isStale) {
        syncFromCache(clusterId, namespace);
        return;
      }

      if (cached && !force) {
        silent = true;
        syncFromCache(clusterId, namespace);
      }

      const inflight = resourceCache.getInflight(clusterId, namespace, listKey);
      if (inflight) {
        await inflight;
        syncFromCache(clusterId, namespace);
        return;
      }

      if (!silent) {
        setKeyLoading(listKey, true);
      } else {
        setKeyRefreshing(listKey, true);
      }

      const request = (async () => {
        try {
          const payload = await getResourceListByType(clusterId, namespace, listKey);
          const items = payload[listKey] || [];
          applyListKey(clusterId, namespace, listKey, items);
          setTabErrors((prev) => {
            if (!prev[listKey]) {
              return prev;
            }
            const next = { ...prev };
            delete next[listKey];
            return next;
          });
          return items;
        } catch (err) {
          const message = err?.message || "Failed to load resources";
          if (!cached) {
            applyListKey(clusterId, namespace, listKey, []);
          }
          setTabErrors((prev) => ({ ...prev, [listKey]: message }));
          throw err;
        } finally {
          setKeyLoading(listKey, false);
          setKeyRefreshing(listKey, false);
        }
      })();

      resourceCache.setInflight(clusterId, namespace, listKey, request);
      await request;
    },
    [clusterId, namespace, applyListKey, syncFromCache, setKeyLoading, setKeyRefreshing]
  );

  const ensureLoaded = useCallback(
    (listKey) => {
      if (!listKey) {
        return;
      }
      void fetchListKey(listKey, { force: false, silent: false });
    },
    [fetchListKey]
  );

  const refreshTab = useCallback(
    (listKey) => {
      if (!listKey) {
        return;
      }
      const cached = resourceCache.get(clusterId, namespace, listKey);
      void fetchListKey(listKey, {
        force: true,
        silent: Boolean(cached?.items?.length),
      });
    },
    [clusterId, namespace, fetchListKey]
  );

  // Reset when cluster or namespace changes.
  useEffect(() => {
    const prev = scopeRef.current;
    if (prev.clusterId === clusterId && prev.namespace === namespace) {
      return;
    }
    if (prev.clusterId && prev.namespace) {
      resourceCache.clearScope(prev.clusterId, prev.namespace);
    }
    scopeRef.current = { clusterId, namespace };
    setResources(emptyNamespaceResources());
    setRawResources(emptyNamespaceResources());
    setLoadingKeys(new Set());
    setRefreshingKeys(new Set());
    setTabErrors({});
  }, [clusterId, namespace]);

  // Load active tab when enabled.
  useEffect(() => {
    if (!enabled || !clusterId || !namespace || !activeListKey) {
      return;
    }
    ensureLoaded(activeListKey);
  }, [enabled, clusterId, namespace, activeListKey, ensureLoaded]);

  // Background refresh for active tab only.
  useEffect(() => {
    if (!enabled || !clusterId || !namespace || !activeListKey) {
      return undefined;
    }

    const timer = window.setInterval(() => {
      const cached = resourceCache.get(clusterId, namespace, activeListKey);
      if (!cached || resourceCache.isStale(cached, RESOURCE_CACHE_TTL_MS)) {
        void fetchListKey(activeListKey, {
          force: false,
          silent: Boolean(cached?.items?.length),
        });
      }
    }, RESOURCE_CACHE_TTL_MS);

    return () => window.clearInterval(timer);
  }, [enabled, clusterId, namespace, activeListKey, fetchListKey]);

  // Re-sync when cache is updated externally (future watch API).
  useEffect(() => {
    return resourceCache.subscribe(() => {
      if (clusterId && namespace) {
        syncFromCache(clusterId, namespace);
      }
    });
  }, [clusterId, namespace, syncFromCache]);

  const isTabLoading = useCallback((listKey) => loadingKeys.has(listKey), [loadingKeys]);
  const isTabRefreshing = useCallback((listKey) => refreshingKeys.has(listKey), [refreshingKeys]);
  const isTabLoaded = useCallback(
    (listKey) => Boolean(resourceCache.get(clusterId, namespace, listKey)),
    [clusterId, namespace]
  );

  const activeTabLoading = activeListKey ? isTabLoading(activeListKey) : false;
  const activeTabRefreshing = activeListKey ? isTabRefreshing(activeListKey) : false;

  return {
    resources,
    rawResources,
    ensureLoaded,
    refreshTab,
    isTabLoading,
    isTabRefreshing,
    isTabLoaded,
    tabErrors,
    activeTabLoading,
    activeTabRefreshing,
  };
}
