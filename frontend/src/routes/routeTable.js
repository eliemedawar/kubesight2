/**
 * The route table — one entry per addressable screen.
 *
 * This replaces the `activePage` switch in App.jsx and the several page-key
 * `Set`s scattered around it (`CLUSTER_CONTEXT_PAGE_KEYS`,
 * `NAMESPACE_CONTEXT_PAGE_KEYS`, `RESOURCE_DATA_PAGES`, `PAGE_LABELS`). Those
 * lists each described one property of a page in a different file; a page could
 * be added to one and forgotten in the others, which is how the cluster-banner
 * suppression list at App.jsx:1812 went stale.
 *
 * Everything the shell needs to know about a screen is one row here, so
 * removing a page is deleting one entry — see ROUTING-AUDIT.md §D.
 *
 * Fields
 *   pageKey    the identifier the rest of the app already speaks: RBAC
 *              (`authz.pageAllowed`), tours (`getTourSteps`), nav highlighting.
 *              Keep it stable — it is not an internal detail.
 *   path       the URL. Static segments outrank dynamic ones in React Router's
 *              own ranking, so `/applications/catalog` wins over
 *              `/applications/:applicationId` without manual ordering.
 *   parent     pageKey whose nav entry stays highlighted on a drill-down.
 *              Replaces the `applicationDetails -> inventory` remap that was
 *              hardcoded at App.jsx:1839.
 *   scope      which topbar selectors this screen uses.
 *                "none"      neither
 *                "cluster"   cluster selector
 *                "namespace" cluster + namespace selectors
 *              Also decides the no-clusters banner: a screen that does not take
 *              a cluster has no business complaining that there isn't one.
 *   loading    Suspense fallback copy while the lazy chunk loads.
 *   hideBundleFab  the change-bundle FAB would cover this screen's own UI.
 */

/** Screens that use the topbar cluster selector but are not nav entries. */
export const SCOPE = {
  NONE: "none",
  CLUSTER: "cluster",
  NAMESPACE: "namespace",
};

export const ROUTES = [
  // ─── Home ───
  {
    pageKey: "dashboard",
    path: "/",
    scope: SCOPE.CLUSTER,
    loading: "Loading dashboard...",
  },

  // ─── Operate ───
  {
    pageKey: "clusters",
    path: "/fleet/clusters",
    scope: SCOPE.CLUSTER,
    loading: "Loading clusters...",
  },
  {
    // Drill-down. Unreachable before routing — see ROUTING-AUDIT.md F3.
    pageKey: "clusterOverview",
    path: "/fleet/clusters/:clusterId",
    parent: "clusters",
    scope: SCOPE.CLUSTER,
    loading: "Loading cluster overview...",
  },
  {
    // Not /fleet/clusters/connections: cluster ids are operator-chosen strings
    // and one could legitimately be named "connections".
    pageKey: "clusterManagement",
    path: "/fleet/connections",
    scope: SCOPE.NONE,
    loading: "Loading cluster management...",
  },
  {
    pageKey: "clusterBuilder",
    path: "/fleet/builder",
    scope: SCOPE.NONE,
    loading: "Loading cluster builder...",
  },
  {
    pageKey: "upgrade",
    path: "/fleet/upgrades",
    scope: SCOPE.CLUSTER,
    loading: "Loading upgrade center...",
  },
  {
    pageKey: "namespaces",
    path: "/workloads",
    scope: SCOPE.NAMESPACE,
    loading: "Loading namespaces...",
  },
  {
    pageKey: "resources",
    path: "/workloads/:clusterId/:namespace",
    parent: "namespaces",
    scope: SCOPE.NAMESPACE,
    loading: "Loading resources...",
    // The FAB sits bottom-right, over this page's own bundle controls.
    hideBundleFab: true,
  },
  {
    pageKey: "topology",
    path: "/topology",
    scope: SCOPE.CLUSTER,
    loading: "Loading topology...",
  },
  {
    pageKey: "logs",
    path: "/logs",
    scope: SCOPE.NAMESPACE,
    loading: "Loading logs...",
  },
  {
    pageKey: "alerts",
    path: "/alerts",
    scope: SCOPE.CLUSTER,
    loading: "Loading alerts...",
  },

  // ─── Applications ───
  {
    pageKey: "inventory",
    path: "/applications",
    scope: SCOPE.CLUSTER,
    loading: "Loading applications...",
  },
  {
    // Drill-down. Unreachable before routing — see ROUTING-AUDIT.md F3.
    // Static siblings below outrank this; see RESERVED_APPLICATION_SEGMENTS.
    pageKey: "applicationDetails",
    path: "/applications/:applicationId",
    parent: "inventory",
    scope: SCOPE.CLUSTER,
    loading: "Loading application details...",
  },
  {
    pageKey: "applicationIntelligence",
    path: "/applications/intelligence",
    scope: SCOPE.NONE,
    loading: "Loading application intelligence...",
  },
  {
    pageKey: "applicationServices",
    path: "/applications/services",
    scope: SCOPE.NONE,
    loading: "Loading app services...",
  },
  {
    pageKey: "serviceCatalog",
    path: "/applications/catalog",
    scope: SCOPE.NONE,
    loading: "Loading service catalog...",
  },
  {
    pageKey: "components",
    path: "/applications/components",
    scope: SCOPE.NONE,
    loading: "Loading components...",
  },
  {
    pageKey: "clients",
    path: "/applications/clients",
    scope: SCOPE.NONE,
    loading: "Loading clients...",
  },
  {
    pageKey: "mobileApps",
    path: "/mobile-apps",
    scope: SCOPE.NONE,
    loading: "Loading mobile applications...",
  },

  // ─── Changes ───
  {
    pageKey: "deploymentRequests",
    path: "/changes/requests",
    scope: SCOPE.NONE,
    loading: "Loading deployment requests...",
  },
  {
    pageKey: "myRequests",
    path: "/changes/my-requests",
    scope: SCOPE.NONE,
    loading: "Loading my requests...",
  },
  {
    pageKey: "changeBundles",
    path: "/changes/bundles",
    scope: SCOPE.NONE,
    loading: "Loading change bundles...",
  },
  {
    // Working *in* a ticketing provider is a job, not a setting. Connecting one
    // is configuration and lives in the integrations hub — same split as the
    // SMTP decision recorded in ROUTING-AUDIT.md F6.
    pageKey: "ticketing",
    path: "/changes/ticketing",
    scope: SCOPE.NONE,
    loading: "Loading ticketing...",
  },

  // ─── Administration ───
  {
    pageKey: "userManagement",
    path: "/admin/users",
    scope: SCOPE.NONE,
    loading: "Loading user management...",
  },
  {
    pageKey: "auditLogs",
    path: "/admin/audit",
    scope: SCOPE.NONE,
    loading: "Loading audit logs...",
  },
  {
    pageKey: "settings",
    path: "/admin/settings",
    scope: SCOPE.NONE,
    loading: "Loading settings...",
  },
  {
    // Hidden from the sidebar (`NAV_PAGES.hidden` in authz.js) because a
    // registry is a connection and is configured in the integrations hub. The
    // route stays so existing deep links and its guided tour still resolve.
    pageKey: "imageRegistries",
    path: "/admin/registries",
    scope: SCOPE.NONE,
    loading: "Loading image registries...",
  },
];

/**
 * Static first segments under /applications that must never be read as an
 * application id. React Router ranks static above dynamic so matching is
 * already correct; this list exists so that adding `/applications/reports`
 * later is a conscious act rather than a silent shadowing of an id.
 */
export const RESERVED_APPLICATION_SEGMENTS = ROUTES.filter(
  (route) => route.path.startsWith("/applications/") && !route.path.includes(":")
).map((route) => route.path.split("/")[2]);

const BY_PAGE_KEY = new Map(ROUTES.map((route) => [route.pageKey, route]));

export function routeForPageKey(pageKey) {
  return BY_PAGE_KEY.get(pageKey) || null;
}

/** Nav entry that should read as active for a screen (drill-downs point at their parent). */
export function navPageKeyFor(pageKey) {
  const route = routeForPageKey(pageKey);
  if (!route) {
    return pageKey;
  }
  return route.parent || route.pageKey;
}

export function routeNeedsClusterContext(pageKey) {
  const scope = routeForPageKey(pageKey)?.scope;
  return scope === SCOPE.CLUSTER || scope === SCOPE.NAMESPACE;
}

export function routeNeedsNamespaceContext(pageKey) {
  return routeForPageKey(pageKey)?.scope === SCOPE.NAMESPACE;
}

export function routeLoadingLabel(pageKey) {
  return routeForPageKey(pageKey)?.loading || "Loading page...";
}

export function routeHidesBundleFab(pageKey) {
  return Boolean(routeForPageKey(pageKey)?.hideBundleFab);
}

/**
 * Which scope values this route names in its path, derived from the path itself
 * so the two can never disagree.
 *
 * A route that names a cluster in its path carries the scope *as identity* —
 * /fleet/clusters/prod-eu is a page about that cluster. Routes that merely
 * filter by one carry it in the query string instead, so the address still
 * round-trips but the path stays stable.
 */
export function routePathParams(pageKey) {
  const path = routeForPageKey(pageKey)?.path || "";
  return {
    clusterId: path.includes(":clusterId"),
    namespace: path.includes(":namespace"),
  };
}
