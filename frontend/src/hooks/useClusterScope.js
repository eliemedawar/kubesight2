import { useCallback, useEffect, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { pathForPageKey } from "../routes/paths.js";
import {
  readScopeFromUrl,
  resolveClusterId,
  resolveNamespace,
  scopeIsPathIdentity,
  scopeSearchParams,
} from "../routes/clusterScope.js";
import { routeNeedsClusterContext, routeNeedsNamespaceContext } from "../routes/routeTable.js";

/**
 * Binds the topbar cluster/namespace selectors to the URL.
 *
 * The selection used to be App state the URL knew nothing about, so a shared
 * link opened on whatever cluster the reader last looked at. It is addressable
 * now: the URL decides what is selected, and changing a selector rewrites it.
 *
 * **The URL is the only writer of the selection.** Selector handlers do not
 * touch state — they change the address, and the effect below follows it. That
 * is not stylistic: an earlier version had the handler set state *and* an effect
 * mirror state back into the URL, which oscillated. Between the handler
 * updating state and the URL catching up there is a render where the two
 * disagree, and since resolution prefers the URL (correct on arrival, wrong
 * mid-update) it kept reverting the change it had just been given. One writer
 * removes the race rather than papering over it with a guard.
 *
 * Where the scope goes depends on the route (see clusterScope.js): the path
 * when the route is *about* that scope, the query string when it merely filters
 * by it. Either way it is a real history entry, so back restores the scope you
 * were looking at and not just the page you were on.
 */

/**
 * pathForPageKey throws when a required param is missing — correct for a nav
 * entry wired wrong at build time, but a selector change must never take the
 * page down.
 */
function safePath(pageKey, params) {
  try {
    return pathForPageKey(pageKey, params);
  } catch {
    return null;
  }
}

export function useClusterScope({
  pageKey,
  routeParams,
  clusters,
  namespaces,
  defaultClusterId,
  selectedClusterId,
  selectedNamespace,
  onClusterChange,
  onNamespaceChange,
}) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const urlScope = useMemo(
    () => readScopeFromUrl({ pageKey, params: routeParams, searchParams }),
    [pageKey, routeParams, searchParams]
  );

  const wantsCluster = routeNeedsClusterContext(pageKey);
  const wantsNamespace = routeNeedsNamespaceContext(pageKey);
  const pathIdentity = scopeIsPathIdentity(pageKey);

  const resolvedCluster = useMemo(() => {
    if (!wantsCluster || !clusters.length) {
      return "";
    }
    return resolveClusterId({
      urlClusterId: urlScope.clusterId,
      currentClusterId: selectedClusterId,
      defaultClusterId,
      clusters,
    });
  }, [wantsCluster, clusters, urlScope.clusterId, selectedClusterId, defaultClusterId]);

  const resolvedNamespace = useMemo(() => {
    if (!wantsNamespace || !namespaces.length) {
      return "";
    }
    return resolveNamespace({
      urlNamespace: urlScope.namespace,
      currentNamespace: selectedNamespace,
      namespaces,
    });
  }, [wantsNamespace, namespaces, urlScope.namespace, selectedNamespace]);

  // URL -> state. The sole writer of the selection.
  useEffect(() => {
    if (resolvedCluster && resolvedCluster !== selectedClusterId) {
      onClusterChange(resolvedCluster);
    }
  }, [resolvedCluster, selectedClusterId, onClusterChange]);

  useEffect(() => {
    if (resolvedNamespace && resolvedNamespace !== selectedNamespace) {
      onNamespaceChange(resolvedNamespace);
    }
  }, [resolvedNamespace, selectedNamespace, onNamespaceChange]);

  // Canonicalisation: put the resolved scope into an address that does not name
  // one, so the first thing a user copies is already shareable.
  //
  // Strictly guarded on the URL being silent. It must never rewrite a scope the
  // URL *does* state — that is the loop described above, and this is the only
  // place a write could reintroduce it.
  useEffect(() => {
    if (!wantsCluster || pathIdentity) {
      return;
    }
    const urlSaysCluster = Boolean(urlScope.clusterId);
    const urlSaysNamespace = Boolean(urlScope.namespace);
    if (urlSaysCluster && (!wantsNamespace || urlSaysNamespace)) {
      return;
    }
    if (!resolvedCluster) {
      return;
    }
    const next = scopeSearchParams({
      pageKey,
      searchParams,
      clusterId: resolvedCluster,
      namespace: resolvedNamespace,
    });
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true });
    }
  }, [
    pageKey,
    wantsCluster,
    wantsNamespace,
    pathIdentity,
    urlScope.clusterId,
    urlScope.namespace,
    resolvedCluster,
    resolvedNamespace,
    searchParams,
    setSearchParams,
  ]);

  /**
   * Selector handlers. These change the address; state follows from the effects
   * above. A push rather than a replace, so back undoes a cluster switch —
   * which is the reason the scope went into the URL in the first place.
   */
  const setCluster = useCallback(
    (clusterId) => {
      if (!clusterId || clusterId === selectedClusterId) {
        return;
      }
      if (pathIdentity) {
        // Carry the current namespace across rather than dropping it. Namespace
        // names repeat across clusters (default, kube-system, and most teams'
        // own names), so the same namespace in the new cluster is usually what
        // was meant; when it does not exist there, resolution picks the first
        // available. Dropping it would mean leaving the page to choose again.
        const path = safePath(pageKey, { ...routeParams, clusterId });
        if (path) {
          navigate(path);
          return;
        }
        onClusterChange(clusterId);
        return;
      }
      setSearchParams(
        scopeSearchParams({ pageKey, searchParams, clusterId, namespace: selectedNamespace })
      );
    },
    [
      pageKey,
      pathIdentity,
      routeParams,
      selectedClusterId,
      selectedNamespace,
      searchParams,
      setSearchParams,
      navigate,
      onClusterChange,
    ]
  );

  const setNamespace = useCallback(
    (namespace) => {
      if (namespace === selectedNamespace) {
        return;
      }
      if (pathIdentity && routeParams?.namespace !== undefined) {
        const path = safePath(pageKey, { ...routeParams, namespace });
        if (path) {
          navigate(path);
          return;
        }
        onNamespaceChange(namespace);
        return;
      }
      setSearchParams(
        scopeSearchParams({
          pageKey,
          searchParams,
          clusterId: selectedClusterId,
          namespace,
        })
      );
    },
    [
      pageKey,
      pathIdentity,
      routeParams,
      selectedClusterId,
      selectedNamespace,
      searchParams,
      setSearchParams,
      navigate,
      onNamespaceChange,
    ]
  );

  return { setCluster, setNamespace };
}
