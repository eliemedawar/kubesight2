import { isAccessDeniedError } from "./authz.js";

/** Distinct UI states for scoped pages (clusters, namespaces, resources, etc.). */
export const ACCESS_VIEW = {
  LOADING: "loading",
  LOADED: "loaded",
  ACCESS_DENIED: "accessDenied",
  ERROR: "error",
  EMPTY: "empty",
};

export function isAuthOrDataLoading({
  authLoading = false,
  coreLoading = false,
  pageLoading = false,
} = {}) {
  return Boolean(authLoading || coreLoading || pageLoading);
}

/**
 * Resolve which high-level view to render after auth/data fetches complete.
 * Loading always wins so empty/unauthorized states never flash during fetch.
 */
export function resolveAccessViewState({
  authLoading = false,
  coreLoading = false,
  pageLoading = false,
  accessError = "",
  empty = false,
  forceAccessDenied = false,
} = {}) {
  if (isAuthOrDataLoading({ authLoading, coreLoading, pageLoading })) {
    return ACCESS_VIEW.LOADING;
  }
  if (forceAccessDenied || isAccessDeniedError(accessError)) {
    return ACCESS_VIEW.ACCESS_DENIED;
  }
  if (String(accessError || "").trim()) {
    return ACCESS_VIEW.ERROR;
  }
  if (empty) {
    return ACCESS_VIEW.EMPTY;
  }
  return ACCESS_VIEW.LOADED;
}

/** True when cluster list is still loading or not yet available for scope checks. */
export function isClusterScopeLoading({ coreLoading = false, authLoading = false } = {}) {
  return isAuthOrDataLoading({ authLoading, coreLoading });
}

/** True when namespace/resource scope is still loading. */
export function isNamespaceScopeLoading({
  coreLoading = false,
  pageLoading = false,
  namespacesLoading = false,
  resourcesLoading = false,
  authLoading = false,
} = {}) {
  return isAuthOrDataLoading({
    authLoading,
    coreLoading,
    pageLoading: pageLoading || namespacesLoading || resourcesLoading,
  });
}

/** Hide global errors while any relevant fetch is still in flight. */
export function shouldDeferAccessMessage({
  authLoading = false,
  coreLoading = false,
  pageLoading = false,
  namespacesLoading = false,
  resourcesLoading = false,
} = {}) {
  return isAuthOrDataLoading({
    authLoading,
    coreLoading,
    pageLoading: pageLoading || namespacesLoading || resourcesLoading,
  });
}

/** Pages that fetch namespace workload lists on demand (active tab or pods only). */
export const RESOURCE_DATA_PAGES = new Set(["resources", "logs"]);

export function pageNeedsResourceData(pageKey) {
  return RESOURCE_DATA_PAGES.has(pageKey);
}

/** Pages that only need the pods list (not full resource tabs). */
export function pageNeedsPodsData(pageKey) {
  return false;
}

export function pageNeedsResourceTabs(pageKey) {
  return pageKey === "resources";
}

/** Contextual loading copy for scoped cluster/namespace/resource fetches. */
export function getScopeLoadingLabel({
  coreLoading = false,
  namespacesLoading = false,
  resourcesLoading = false,
  pageLoading = false,
} = {}) {
  if (coreLoading) {
    return "Loading clusters...";
  }
  if (resourcesLoading) {
    return "Loading resources...";
  }
  if (namespacesLoading) {
    return "Loading namespaces...";
  }
  if (pageLoading) {
    return "Loading...";
  }
  return "Loading...";
}

export const SCOPE_LOADING_HINT =
  "Fetching live data from the cluster — this may take a moment on large environments.";
