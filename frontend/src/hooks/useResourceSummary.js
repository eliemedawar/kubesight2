import { useCallback, useEffect, useRef, useState } from "react";
import { getResourceSummary } from "../api/clustersApi.js";
import { setVisibleInterval } from "../lib/visibleInterval";

// The rail's counts change slowly next to the open tab (which polls every few
// seconds on its own), and one summary reads every kind in the namespace.
export const RESOURCE_SUMMARY_INTERVAL_MS = 30000;

const EMPTY = { kinds: {}, attention: [], attentionTotal: 0, unavailable: [] };

/**
 * Counts and problems per kind for one namespace (GET …/resource-summary).
 * Re-read every 30 s while `live` and the browser tab is visible; `refresh()`
 * re-reads at once (the page calls it with its own Refresh button).
 */
export function useResourceSummary({ clusterId, namespace, enabled = true, live = true }) {
  const [summary, setSummary] = useState(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const scopeRef = useRef("");

  const load = useCallback(
    async () => {
      if (!enabled || !clusterId || !namespace) {
        return;
      }
      const scope = `${clusterId}/${namespace}`;
      try {
        const payload = await getResourceSummary(clusterId, namespace);
        // A slow answer for the namespace the viewer just left must not land.
        if (scopeRef.current !== scope) {
          return;
        }
        setSummary({ ...EMPTY, ...(payload || {}) });
        setLoaded(true);
      } catch {
        // The rail is a guide, not the data: on failure it keeps the last
        // counts (or none), and every tab still loads on its own.
      }
    },
    [enabled, clusterId, namespace]
  );

  useEffect(() => {
    scopeRef.current = `${clusterId}/${namespace}`;
    setSummary(EMPTY);
    setLoaded(false);
    load();
  }, [clusterId, namespace, load]);

  useEffect(() => {
    if (!enabled || !live || !clusterId || !namespace) {
      return undefined;
    }
    return setVisibleInterval(() => {
      load();
    }, RESOURCE_SUMMARY_INTERVAL_MS);
  }, [enabled, live, clusterId, namespace, load]);

  const refresh = useCallback(() => {
    load();
  }, [load]);

  return { summary, loaded, refresh };
}
