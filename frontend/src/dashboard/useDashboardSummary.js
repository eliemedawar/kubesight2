import { useCallback, useEffect, useRef, useState } from "react";
import { getDashboardSummary } from "../api/dashboardApi.js";
import { formatAccessError, shouldShowAccessError } from "../utils/authz.js";

/**
 * The dashboard's own data.
 *
 * Lifted out of App, where it was one of eleven effects keyed on the active
 * page. Two things get better by moving it rather than only by tidying it:
 *
 *   The poller's lifetime becomes the route's lifetime. It used to be started
 *   and stopped by an effect that had to check `page === "dashboard"` on every
 *   run; now leaving the dashboard unmounts the component and React stops the
 *   interval. There is no page check left to get wrong.
 *
 *   The stale-response guards stay next to the request they guard. Both are
 *   load-bearing and neither is obvious: a sequence number, because a slow
 *   response from an earlier poll must not overwrite a newer one, and a cluster
 *   check, because switching cluster mid-flight would otherwise paint the old
 *   cluster's numbers under the new cluster's name.
 *
 * `onClusterMissing` is the one thing this cannot own. A 404 means the cluster
 * was deleted — often in another tab — and the right response is to refresh the
 * cluster list so it drops away and a valid one is selected. That list belongs
 * to the shell, so the hook reports and the shell decides.
 */
export function useDashboardSummary({
  clusterId,
  enabled = true,
  refreshIntervalSeconds,
  canAccessCluster,
  onClusterMissing,
}) {
  const [summary, setSummary] = useState(null);
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  // Newest request wins. Without this a slow poll landing after a fast one
  // repaints older numbers over newer ones, which on a page people watch for
  // change is worse than no update at all.
  const seqRef = useRef(0);
  // Which cluster the in-flight request belongs to.
  const clusterRef = useRef("");
  // Which cluster the *displayed* summary belongs to, so a cluster switch
  // clears the old numbers instead of leaving them under the new name.
  const shownClusterRef = useRef("");

  const load = useCallback(
    async (targetClusterId, { background = false } = {}) => {
      if (!targetClusterId || !canAccessCluster?.(targetClusterId)) {
        clusterRef.current = "";
        setSummary(null);
        setRefreshedAt(null);
        setRefreshing(false);
        setLoading(false);
        return;
      }

      const seq = ++seqRef.current;
      const isLatest = () => seq === seqRef.current;
      clusterRef.current = targetClusterId;

      if (background) {
        setRefreshing(true);
      } else {
        setLoading(true);
      }
      setError("");

      try {
        const next = await getDashboardSummary(targetClusterId);
        if (!isLatest() || clusterRef.current !== targetClusterId) {
          return;
        }
        setSummary(next);
        setRefreshedAt(new Date().toISOString());
      } catch (loadError) {
        if (!isLatest() || clusterRef.current !== targetClusterId) {
          return;
        }
        if (loadError.status === 404) {
          // The cluster is gone. Stop showing its numbers and ask the shell to
          // re-resolve the list rather than polling a dead id.
          clusterRef.current = "";
          shownClusterRef.current = "";
          setSummary(null);
          setRefreshedAt(null);
          onClusterMissing?.();
          return;
        }
        if (shouldShowAccessError(loadError.message, {
          expectedDenied: !canAccessCluster?.(targetClusterId),
        })) {
          setError(formatAccessError(loadError.message));
        }
      } finally {
        if (isLatest()) {
          if (background) {
            setRefreshing(false);
          } else {
            setLoading(false);
          }
        }
      }
    },
    [canAccessCluster, onClusterMissing]
  );

  useEffect(() => {
    if (!enabled || !clusterId) {
      return undefined;
    }

    if (shownClusterRef.current && shownClusterRef.current !== clusterId) {
      setSummary(null);
      setRefreshedAt(null);
    }
    shownClusterRef.current = clusterId;

    load(clusterId);

    // Clamped to 30–60s. Below 30 the summary is expensive enough to matter on
    // a large cluster; above 60 it stops reading as live.
    const seconds = Math.min(Math.max(Number(refreshIntervalSeconds) || 30, 30), 60);
    const timer = window.setInterval(() => load(clusterId, { background: true }), seconds * 1000);
    return () => window.clearInterval(timer);
  }, [enabled, clusterId, refreshIntervalSeconds, load]);

  const refresh = useCallback(
    () => load(clusterId, { background: summary?.clusterId === clusterId }),
    [load, clusterId, summary?.clusterId]
  );

  return { summary, refreshedAt, loading, refreshing, error, refresh };
}
