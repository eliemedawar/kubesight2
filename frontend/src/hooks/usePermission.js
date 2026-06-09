import { useAuth } from "../context/AuthContext";

/** Permission helpers from the authenticated user profile. */
export function usePermission() {
  const auth = useAuth();
  return {
    hasPermission: auth.hasPermission,
    hasAnyPermission: auth.hasAnyPermission,
    isAdmin: auth.isAdmin,
    canAccessCluster: auth.canAccessCluster,
    canAccessNamespace: auth.canAccessNamespace,
    canAccessResource: auth.canAccessResource,
    canViewLogs: auth.canViewLogs,
    canViewServicePort: auth.canViewServicePort,
    getAllowedClusters: auth.getAllowedClusters,
    getAllowedNamespaces: auth.getAllowedNamespaces,
    getAllowedResources: auth.getAllowedResources,
    getLogVisiblePods: auth.getLogVisiblePods,
    hasAnyClusterAccess: auth.hasAnyClusterAccess,
    hasAnyNamespaceAccess: auth.hasAnyNamespaceAccess,
    canViewResourceTab: auth.canViewResourceTab,
    getVisibleResourceTabs: auth.getVisibleResourceTabs,
    pageNeedsClusterContext: auth.pageNeedsClusterContext,
    pageNeedsNamespaceContext: auth.pageNeedsNamespaceContext,
    pageAllowed: auth.pageAllowed,
    getVisiblePages: auth.getVisiblePages,
    getFirstAllowedPage: auth.getFirstAllowedPage,
    shouldShowAccessError: auth.shouldShowAccessError,
    formatAccessError: auth.formatAccessError,
  };
}
