/**
 * The information architecture: which sidebar group each page lives in, and
 * which pages are folded into a single workspace entry with its own tab strip.
 *
 * Pure data plus pure helpers — whether a page is *allowed* stays in
 * utils/authz.js (NAV_PAGES + pageAllowed); this file only decides where an
 * allowed page is shown. Groups follow the delivery lifecycle: build it, ship
 * it, run it, watch it, administer it.
 */

/**
 * A workspace is one sidebar entry holding several pages as tabs. Its page
 * keys keep their own routes, permissions and tours, so a deep link to any of
 * them still resolves; the sidebar lights the workspace instead.
 */
export const WORKSPACES = {
  buildCenter: {
    label: "Build Center",
    description: "Build pipelines, source analysis and mobile releases",
    tabs: [
      { pageKey: "serviceCatalog", label: "CI Services" },
      { pageKey: "applicationIntelligence", label: "Application Intelligence" },
      { pageKey: "mobileApps", label: "Mobile Apps" },
    ],
  },
  architecture: {
    label: "Service Architecture",
    shortLabel: "Architecture",
    description: "Blueprints, running app services, clients and components",
    tabs: [
      { pageKey: "blueprints", label: "Blueprints" },
      { pageKey: "applicationServices", label: "App Services" },
      { pageKey: "clients", label: "Clients" },
      { pageKey: "components", label: "Components" },
    ],
  },
};

/**
 * Sidebar layout. An item is either `{ page }` (one page) or
 * `{ workspace }` (a WORKSPACES key). `label` overrides the NAV_PAGES label
 * for the sidebar only.
 */
export const NAV_GROUPS = [
  {
    id: "overview",
    label: "",
    items: [{ page: "dashboard" }],
  },
  {
    id: "applications",
    label: "Applications",
    items: [{ workspace: "buildCenter" }, { workspace: "architecture" }],
  },
  {
    id: "delivery",
    label: "Delivery",
    items: [
      { page: "inventory" },
      { page: "myRequests" },
      { page: "changeBundles" },
      { page: "deploymentRequests", label: "Approvals" },
      { page: "ticketing" },
    ],
  },
  {
    id: "infrastructure",
    label: "Infrastructure",
    items: [
      { page: "clusters" },
      { page: "namespaces" },
      { page: "resources" },
      { page: "clusterManagement" },
      { page: "clusterBuilder" },
      { page: "upgrade" },
    ],
  },
  {
    id: "observability",
    label: "Observability",
    items: [{ page: "alerts" }, { page: "logs" }, { page: "auditLogs" }],
  },
  {
    id: "administration",
    label: "Administration",
    items: [
      { page: "userManagement", label: "Users & Roles" },
      { page: "apiTokens" },
      { page: "integrations" },
      { page: "settings" },
    ],
  },
];

const WORKSPACE_OF_PAGE = new Map(
  Object.entries(WORKSPACES).flatMap(([key, ws]) =>
    ws.tabs.map((tab) => [tab.pageKey, key])
  )
);

/** The workspace key a page is folded into, or null. */
export function workspaceOfPage(pageKey) {
  return WORKSPACE_OF_PAGE.get(pageKey) || null;
}

/** The sidebar entry key that lights up for a nav key from the router. */
export function sidebarKeyFor(navKey) {
  return workspaceOfPage(navKey) || navKey;
}

/**
 * Resolve the layout against the pages this user may see.
 *
 * `visiblePages` is authz's getVisiblePages() output. Returns groups whose
 * items carry `{ key, label, pageKey, tabs? }` where `pageKey` is the page a
 * click opens (a workspace's first allowed tab). Empty items and groups drop.
 * Any visible page the layout forgot is appended to a trailing group rather
 * than silently vanishing from the sidebar.
 */
export function buildNavGroups(visiblePages) {
  const byKey = new Map(visiblePages.map((page) => [page.key, page]));
  const placed = new Set();

  const groups = NAV_GROUPS.map((group) => {
    const items = group.items
      .map((item) => {
        if (item.workspace) {
          const ws = WORKSPACES[item.workspace];
          const tabs = ws.tabs.filter((tab) => byKey.has(tab.pageKey));
          tabs.forEach((tab) => placed.add(tab.pageKey));
          if (!tabs.length) return null;
          return {
            key: item.workspace,
            label: ws.shortLabel || ws.label,
            pageKey: tabs[0].pageKey,
            tabs,
          };
        }
        const page = byKey.get(item.page);
        if (!page) return null;
        placed.add(page.key);
        return { key: page.key, label: item.label || page.label, pageKey: page.key };
      })
      .filter(Boolean);
    return { id: group.id, label: group.label, items };
  }).filter((group) => group.items.length);

  const orphans = visiblePages.filter((page) => !placed.has(page.key));
  if (orphans.length) {
    groups.push({
      id: "more",
      label: "More",
      items: orphans.map((page) => ({ key: page.key, label: page.label, pageKey: page.key })),
    });
  }
  return groups;
}

/**
 * Flat, searchable list of every destination (pages and workspace tabs) for
 * the jump palette. Each entry: `{ id, label, context, pageKey }`.
 */
export function buildJumpTargets(groups) {
  const targets = [];
  groups.forEach((group) => {
    group.items.forEach((item) => {
      if (item.tabs) {
        item.tabs.forEach((tab) => {
          targets.push({
            id: `${item.key}:${tab.pageKey}`,
            label: tab.label,
            context: [group.label, item.label].filter(Boolean).join(" › "),
            pageKey: tab.pageKey,
            navKey: item.key,
          });
        });
      } else {
        targets.push({
          id: item.key,
          label: item.label,
          context: group.label || "Overview",
          pageKey: item.pageKey,
          navKey: item.key,
        });
      }
    });
  });
  return targets;
}
