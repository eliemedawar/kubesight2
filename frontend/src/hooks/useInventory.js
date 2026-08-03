import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getInventoryDetail, listInventory } from "../api/inventoryApi.js";
import { describeLoadError } from "../utils/authz.js";

/**
 * Cluster options for the inventory screens.
 *
 * Audit item E5, and it turned out to be smaller than it looked. App fetched
 * the whole inventory list on every visit to /applications, but never passed
 * the items anywhere — `InventoryPage` renders templates and Helm releases, not
 * rows. The list existed only to derive this dropdown, plus a namespace option
 * list that was computed and passed to nothing at all.
 *
 * The fetch is kept because it earns one thing: an application can live in a
 * cluster that is not in the user's allowed list, and the dropdown should still
 * name it rather than showing a blank. That is the entire contribution, so it
 * lives next to the dropdown instead of in the shell.
 */
export function useInventoryClusterOptions({ clusterId, allowedClusters = [], enabled = true }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!enabled) {
      setItems([]);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const result = await listInventory(clusterId ? { cluster: clusterId } : undefined);
      setItems(Array.isArray(result) ? result : []);
    } catch (loadError) {
      setError(describeLoadError(loadError.message));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [clusterId, enabled]);

  useEffect(() => {
    load();
  }, [load]);

  const options = useMemo(() => {
    const byId = new Map();
    allowedClusters.forEach((cluster) => {
      byId.set(cluster.id, { id: cluster.id, name: cluster.name || cluster.id });
    });
    items.forEach((item) => {
      const id = item.cluster || item.clusterId;
      if (id && !byId.has(id)) {
        byId.set(id, { id, name: id });
      }
    });
    return [...byId.values()].sort((a, b) => String(a.name).localeCompare(String(b.name)));
  }, [items, allowedClusters]);

  return { options, loading, error, reload: load };
}

/**
 * One application's detail, keyed on the id in the URL.
 *
 * Audit item E6. The request guard matters more here than it did in App: the id
 * comes from the path now, so it can change as fast as a click, and without the
 * sequence check a slow response for the previous application would paint its
 * data under the new one's name.
 */
export function useApplicationDetail(applicationId) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    if (!applicationId) {
      setDetail(null);
      return;
    }
    const requestId = ++requestRef.current;
    setDetail(null);
    setLoading(true);
    setError("");
    try {
      const result = await getInventoryDetail(applicationId);
      if (requestId !== requestRef.current) {
        return;
      }
      setDetail(result);
    } catch (loadError) {
      if (requestId !== requestRef.current) {
        return;
      }
      setError(describeLoadError(loadError.message));
      setDetail(null);
    } finally {
      if (requestId === requestRef.current) {
        setLoading(false);
      }
    }
  }, [applicationId]);

  useEffect(() => {
    load();
  }, [load]);

  return { detail, loading, error, reload: load };
}
