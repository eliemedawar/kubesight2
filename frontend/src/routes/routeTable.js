/**
 * The route table: one row per addressable view.
 *
 * Pure data plus the slug helpers — no React, no DOM, no imports that touch
 * either, so `routeUrl.js` and its tests can run under the node test env.
 *
 * Fields
 *   key       unique id for the route row itself
 *   path      pattern; `:name` is a param, `:name?` is an optional trailing param
 *   pageKey   the authz/render key (utils/authz.js). Defaults to `key`
 *   navKey    which sidebar entry highlights. Defaults to `pageKey`
 *   params    per-param descriptor: { values } for an enumerated param,
 *             omitted for a free-form id
 *   defaults  value a param falls back to, and which is omitted from the URL
 *   query     the ONLY query keys this route carries. Anything else in the URL
 *             is dropped on parse — that is what stops `?next=http://evil`
 *
 * Ordering: more specific paths come before the patterns that could otherwise
 * swallow them. `routeTable.test.js` asserts no row shadows a later one.
 */

import { RESOURCE_TAB_DEFINITIONS } from "../lib/resourceTypes.js";

/** URL form of an enumerated value: lowercase, non-alphanumerics to dashes. */
export function slugifyValue(value) {
  return String(value)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export const RESOURCE_TAB_KEYS = RESOURCE_TAB_DEFINITIONS.map((tab) => tab.tabKey);

/** Tab vocabularies, copied from the pages that own them. */
export const TAB_VALUES = {
  // pages/ApplicationDetailsPage.jsx:44
  applicationDetails: [
    "overview",
    "pods",
    "resources",
    "versions",
    "yaml",
    "logs",
    "events",
    "helm",
    "actions",
  ],
  // pages/AlertsPage.jsx:341
  alerts: ["open", "history", "policies"],
  // pages/ServiceDetailPage.jsx:20
  serviceDetail: [
    "overview",
    "source",
    "application",
    "pipeline",
    // Slugifies to "mergechecks" in the URL and resolves back to this. A tab
    // missing from this list is not a tab with an ugly URL — the whole route
    // fails to parse and the app lands on the dashboard.
    "mergeChecks",
    "dockerfile",
    "builds",
    "artifacts",
    "settings",
  ],
  // pages/ApplicationIntelligencePage.jsx:72 — spaces and "&" are slugified
  applicationIntelligence: [
    "Overview",
    "Findings",
    "Architecture",
    "APIs",
    "Configuration",
    "Container & build",
    "Deployment",
    "History",
  ],
  // components/ticketing/ProviderWorkspace.jsx:20
  ticketing: ["overview", "fieldsync", "tickets"],
  // pages/ClusterBuilderPage.jsx:139
  clusterBuilder: ["floor", "new", "sources"],
  // pages/InventoryPage.jsx:53
  inventory: ["templates", "helm"],
  // pages/UserManagementPage.jsx:122
  userManagement: ["users", "roles"],
  // pages/MyRequestsPage.jsx:9 and pages/DeploymentRequestsPage.jsx:21
  requests: ["active", "history"],
  // pages/ChangeBundlesPage.jsx:243 — the visible set is RBAC-filtered, the
  // page falls back when a tab is not available to this user
  changeBundles: ["mine", "pending", "all"],
  // lib/settingsSections.js — the preference sections, plus the Administration
  // rows that are PANELS of this page. The remaining Administration rows are
  // `link:` navigations to other pages and are not sections of this one.
  settings: [
    "profile",
    "appearance",
    "workspace",
    "notifications",
    "security",
    "mergeChecks",
  ],
  // pages/ApplicationServicesPage.jsx:1440
  applicationServices: ["overview", "dr"],
  // Resource tabs are RBAC-filtered at render time; the full static set is
  // valid in a URL and ResourcesPage falls back if this user cannot see it.
  resources: RESOURCE_TAB_KEYS,
};

export const ROUTES = [
  { key: "dashboard", path: "/dashboard", query: ["cluster"] },

  // Infrastructure
  { key: "clusters", path: "/clusters", query: ["cluster"] },
  {
    key: "clusterOverview",
    path: "/clusters/:clusterId/overview",
    navKey: "clusters",
  },
  { key: "clusterManagement", path: "/cluster-management" },
  { key: "namespaces", path: "/namespaces", query: ["cluster", "ns"] },
  {
    key: "resources",
    path: "/resources/:tab?",
    params: { tab: { values: TAB_VALUES.resources } },
    defaults: { tab: "pods" },
    query: ["cluster", "ns"],
  },

  // Inventory — the app drill-down is listed first so it is not swallowed
  // by /inventory/:section
  {
    key: "applicationDetails",
    path: "/inventory/app/:appId/:tab?",
    navKey: "inventory",
    params: { tab: { values: TAB_VALUES.applicationDetails } },
    defaults: { tab: "overview" },
    query: ["cluster"],
  },
  {
    key: "inventory",
    path: "/inventory/:section?",
    params: { section: { values: TAB_VALUES.inventory } },
    defaults: { section: "templates" },
    query: ["cluster"],
  },
  {
    key: "myRequests",
    path: "/my-requests/:tab?",
    params: { tab: { values: TAB_VALUES.requests } },
    defaults: { tab: "active" },
  },
  {
    key: "changeBundles",
    path: "/change-bundles/:tab?",
    params: { tab: { values: TAB_VALUES.changeBundles } },
    defaults: { tab: "mine" },
  },

  // Monitoring
  { key: "logs", path: "/logs", query: ["cluster", "ns", "pod"] },
  {
    key: "alerts",
    path: "/alerts/:tab?",
    params: { tab: { values: TAB_VALUES.alerts } },
    defaults: { tab: "open" },
    query: ["cluster"],
  },

  // Services
  {
    key: "serviceDetail",
    path: "/service-catalog/:serviceId/:tab?",
    pageKey: "serviceCatalog",
    params: { tab: { values: TAB_VALUES.serviceDetail } },
    defaults: { tab: "overview" },
    query: ["build"],
  },
  { key: "serviceCatalog", path: "/service-catalog" },
  { key: "blueprintDetail", path: "/blueprints/:blueprintId", pageKey: "blueprints" },
  { key: "blueprints", path: "/blueprints" },
  {
    key: "applicationServiceDetail",
    path: "/app-services/:serviceId/:tab?",
    pageKey: "applicationServices",
    params: { tab: { values: TAB_VALUES.applicationServices } },
    defaults: { tab: "overview" },
  },
  { key: "applicationServices", path: "/app-services" },
  {
    key: "applicationIntelligenceDetail",
    path: "/application-intelligence/:appId/:tab?",
    pageKey: "applicationIntelligence",
    params: { tab: { values: TAB_VALUES.applicationIntelligence } },
    defaults: { tab: "Overview" },
  },
  { key: "applicationIntelligence", path: "/application-intelligence" },
  { key: "components", path: "/components" },
  { key: "clientDetail", path: "/clients/:clientId", pageKey: "clients" },
  { key: "clients", path: "/clients" },

  // Administration
  {
    key: "userManagement",
    path: "/users/:tab?",
    params: { tab: { values: TAB_VALUES.userManagement } },
    defaults: { tab: "users" },
  },
  { key: "auditLogs", path: "/audit-logs" },
  { key: "apiTokens", path: "/api-tokens" },
  {
    key: "deploymentRequests",
    path: "/deployment-requests/:tab?",
    params: { tab: { values: TAB_VALUES.requests } },
    defaults: { tab: "active" },
  },
  { key: "imageRegistries", path: "/image-registries" },
  {
    key: "ticketingProvider",
    path: "/ticketing/:provider/:tab?",
    pageKey: "ticketing",
    params: { tab: { values: TAB_VALUES.ticketing } },
    defaults: { tab: "overview" },
  },
  { key: "ticketing", path: "/ticketing" },
  {
    key: "integrationDetail",
    path: "/integrations/:integrationKey",
    pageKey: "integrations",
  },
  { key: "integrations", path: "/integrations" },
  {
    key: "settings",
    path: "/settings/:section?",
    params: { section: { values: TAB_VALUES.settings } },
    defaults: { section: "profile" },
  },

  // Operations
  { key: "mobileAppDetail", path: "/mobile-apps/:appId", pageKey: "mobileApps" },
  { key: "mobileApps", path: "/mobile-apps" },
  {
    key: "clusterBuildDetail",
    path: "/cluster-builder/builds/:buildId",
    pageKey: "clusterBuilder",
  },
  {
    key: "clusterBuilder",
    path: "/cluster-builder/:tab?",
    params: { tab: { values: TAB_VALUES.clusterBuilder } },
    defaults: { tab: "floor" },
  },
  { key: "upgrade", path: "/upgrade", query: ["cluster"] },
];

export const DEFAULT_ROUTE_KEY = "dashboard";

const BY_KEY = new Map(ROUTES.map((route) => [route.key, route]));

export function routeByKey(key) {
  return BY_KEY.get(key) || null;
}

/** The authz/render key a route row resolves to. */
export function pageKeyOf(route) {
  return route.pageKey || route.key;
}

/** The sidebar entry a route row highlights. */
export function navKeyOf(route) {
  return route.navKey || pageKeyOf(route);
}

/**
 * The route row to use when navigating by bare page key — the list view, not a
 * drill-down. Falls back to any row that renders that page.
 */
export function routeForPageKey(pageKey) {
  return (
    ROUTES.find((route) => route.key === pageKey) ||
    ROUTES.find((route) => pageKeyOf(route) === pageKey) ||
    null
  );
}

/** Parsed path pattern: literals and params, in order. */
export function patternSegments(path) {
  return path
    .split("/")
    .filter(Boolean)
    .map((segment) => {
      if (!segment.startsWith(":")) {
        return { literal: segment };
      }
      const optional = segment.endsWith("?");
      return { name: segment.slice(1, optional ? -1 : undefined), optional };
    });
}

/**
 * slug -> raw value for an enumerated param, so "container-build" resolves back
 * to "Container & build". Built once per route+param.
 */
const slugCache = new WeakMap();

export function slugMapFor(descriptor) {
  if (!descriptor?.values) {
    return null;
  }
  let cached = slugCache.get(descriptor);
  if (!cached) {
    cached = new Map(descriptor.values.map((value) => [slugifyValue(value), value]));
    slugCache.set(descriptor, cached);
  }
  return cached;
}
