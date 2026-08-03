/**
 * Where the selected cluster and namespace live in a URL, and how to resolve
 * them.
 *
 * Two carriers, chosen per route:
 *
 *   path   the route is *about* that scope. /fleet/clusters/prod-eu and
 *          /workloads/prod-eu/kube-system name their subject; changing the
 *          selector is navigation, and back should undo it.
 *   query  the route merely filters by scope. /alerts?cluster=prod-eu keeps the
 *          path stable while still round-tripping through a bookmark.
 *
 * Everything here is pure so the precedence rules can be tested without a
 * router, which matters because they are the part that decides whether a shared
 * link opens on what the sender was looking at.
 */

import { routeNeedsClusterContext, routeNeedsNamespaceContext, routePathParams } from "./routeTable.js";

export const CLUSTER_PARAM = "cluster";
export const NAMESPACE_PARAM = "namespace";

/**
 * The scope a URL asks for. Null for "the URL does not say", which is different
 * from "" — the caller falls back to its remembered selection only in the
 * former case.
 */
export function readScopeFromUrl({ pageKey, params = {}, searchParams } = {}) {
  const carriers = routePathParams(pageKey);
  const get = (key) => {
    if (!searchParams) {
      return null;
    }
    const value = typeof searchParams.get === "function" ? searchParams.get(key) : null;
    return value || null;
  };

  return {
    clusterId: carriers.clusterId ? params.clusterId || null : get(CLUSTER_PARAM),
    namespace: carriers.namespace ? params.namespace || null : get(NAMESPACE_PARAM),
  };
}

/**
 * Resolve the cluster to select, in precedence order:
 *
 *   1. what the URL says, if that cluster is one the user can actually reach
 *   2. the current selection, if still valid
 *   3. the workspace default
 *   4. the first cluster available
 *
 * A URL naming a cluster the user cannot see falls through rather than pinning
 * an empty selection: they get a working page scoped to something they can see,
 * and the cluster-level authorization error surfaces from the API rather than
 * from a blank selector. Deliberate — a stale bookmark after a permission
 * change is common, and the page should still work.
 */
export function resolveClusterId({
  urlClusterId,
  currentClusterId,
  defaultClusterId,
  clusters = [],
} = {}) {
  const exists = (id) => Boolean(id) && clusters.some((cluster) => cluster.id === id);
  if (exists(urlClusterId)) {
    return urlClusterId;
  }
  if (exists(currentClusterId)) {
    return currentClusterId;
  }
  if (exists(defaultClusterId)) {
    return defaultClusterId;
  }
  return clusters[0]?.id || "";
}

/** Same precedence for namespaces, against the namespace list of the chosen cluster. */
export function resolveNamespace({
  urlNamespace,
  currentNamespace,
  namespaces = [],
} = {}) {
  const names = namespaces.map((ns) => (typeof ns === "string" ? ns : ns?.name)).filter(Boolean);
  const exists = (name) => Boolean(name) && names.includes(name);
  if (exists(urlNamespace)) {
    return urlNamespace;
  }
  if (exists(currentNamespace)) {
    return currentNamespace;
  }
  return names[0] || "";
}

/**
 * The query string a route should carry for a given scope.
 *
 * Only writes the params this route actually uses: a namespace on /alerts would
 * be noise in a shared link, and a cluster on /admin/users would imply the page
 * respects a scope it ignores. Existing unrelated params are preserved.
 */
export function scopeSearchParams({ pageKey, searchParams, clusterId, namespace } = {}) {
  const next = new URLSearchParams(searchParams || "");
  const carriers = routePathParams(pageKey);

  const wantsClusterQuery = routeNeedsClusterContext(pageKey) && !carriers.clusterId;
  const wantsNamespaceQuery = routeNeedsNamespaceContext(pageKey) && !carriers.namespace;

  if (wantsClusterQuery && clusterId) {
    next.set(CLUSTER_PARAM, clusterId);
  } else {
    next.delete(CLUSTER_PARAM);
  }

  if (wantsNamespaceQuery && namespace) {
    next.set(NAMESPACE_PARAM, namespace);
  } else {
    next.delete(NAMESPACE_PARAM);
  }

  return next;
}

/** True when a scope change on this route means navigating rather than filtering. */
export function scopeIsPathIdentity(pageKey) {
  const carriers = routePathParams(pageKey);
  return carriers.clusterId || carriers.namespace;
}
