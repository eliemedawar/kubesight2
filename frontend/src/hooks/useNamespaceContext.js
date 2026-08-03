import { useCallback, useEffect, useRef, useState } from "react";
import { listNamespaceMetricsByCluster, listNamespacesByCluster } from "../api";
import { resourceCache } from "../services/resourceCacheService.js";

/**
 * Loads the namespace list for a cluster.
 *
 * Extracted from the 167-line effect in App.jsx that also fetched the cluster
 * overview and was keyed on the active page. Two things came apart here:
 *
 *   - The overview fetch moved out. It was bundled into the same
 *     Promise.allSettled to save a round-trip, but the two were already running
 *     in parallel, so splitting them costs nothing and removes a branch that
 *     made a namespace loader care which page was open.
 *   - The page-key check became a scope check. Namespaces are needed by any
 *     cluster-scoped screen; that is a property of the route, not a list of
 *     page names to keep in sync.
 *
 * The phased load is preserved exactly, because it is the reason the page is
 * usable on large clusters: lite (one kubectl call) paints the namespace
 * selector immediately, counts merges in next, and metrics merges in whenever
 * it arrives — so a slow or absent metrics-server can never hold up or blank
 * the numbers. Counts and metrics carry disjoint fields, so merging by name is
 * order-independent and whichever lands first is never clobbered.
 */
export function useNamespaceContext({ clusterId, enabled, onError }) {
  const [namespaces, setNamespaces] = useState([]);
  const [loading, setLoading] = useState(false);

  // Which cluster the current list belongs to. Guards the reload: before, any
  // page change re-ran the whole effect, and this ref is what stopped it from
  // re-fetching namespaces that were already in hand.
  const loadedClusterRef = useRef("");

  const reset = useCallback(() => {
    loadedClusterRef.current = "";
    setNamespaces([]);
  }, []);

  useEffect(() => {
    if (!enabled || !clusterId) {
      loadedClusterRef.current = "";
      return undefined;
    }
    if (loadedClusterRef.current === clusterId) {
      return undefined;
    }

    let cancelled = false;

    const load = async () => {
      // A different cluster's resources must not survive the switch.
      resourceCache.clearAll();
      setNamespaces([]);
      setLoading(true);

      const countsPromise = listNamespacesByCluster(clusterId, { counts: true });
      let result;
      try {
        result = await listNamespacesByCluster(clusterId, { lite: true });
      } catch (liteError) {
        // Lite failed — fall back to the counts request before reporting.
        try {
          result = await countsPromise;
        } catch {
          if (!cancelled) {
            loadedClusterRef.current = "";
            setNamespaces([]);
            setLoading(false);
            onError?.(liteError);
          }
          return;
        }
      }

      if (cancelled) {
        return;
      }

      const items = result.items || [];
      setNamespaces(items);
      setLoading(false);
      loadedClusterRef.current = clusterId;

      const stillCurrent = () => !cancelled && loadedClusterRef.current === clusterId;
      const merge = (incoming) => {
        if (!incoming?.length || !stillCurrent()) {
          return;
        }
        setNamespaces((prev) => {
          const byName = new Map(incoming.map((ns) => [ns.name, ns]));
          return prev.map((ns) => (byName.has(ns.name) ? { ...ns, ...byName.get(ns.name) } : ns));
        });
      };

      countsPromise.then((res) => merge(res.items)).catch(() => {});
      listNamespaceMetricsByCluster(clusterId)
        .then((res) => merge(res.items))
        .catch(() => {});
    };

    load();
    return () => {
      cancelled = true;
      setLoading(false);
    };
  }, [clusterId, enabled, onError]);

  return { namespaces, loading, reset };
}
