/**
 * The five navigation groups.
 *
 * Grouping lives here rather than as a `section` string on each `NAV_PAGES`
 * entry, because grouping is a product decision about how operators think and
 * `authz.js` is about who may see what. They changed at different times and for
 * different reasons, and the seven ad-hoc sections that resulted — Dashboard,
 * Infrastructure, Inventory, Monitoring, Services, Administration, Operations —
 * split related work across menus: Upgrade Center sat in "Operations" while the
 * clusters it upgrades sat in "Infrastructure".
 *
 * Five groups, ordered by how often an operator needs them:
 *
 *   Home            what needs attention right now
 *   Operate         the running fleet, and acting on it
 *   Applications    what is deployed and what it is made of
 *   Changes         work moving through approval
 *   Administration  people, history, configuration
 *
 * Order within a group is the array order here, deliberately, not the order
 * pages happen to appear in `NAV_PAGES`.
 *
 * A page listed here but not permitted is dropped by the caller; a page in no
 * group never appears in the sidebar at all, which is how drill-downs
 * (`clusterOverview`, `applicationDetails`) and `imageRegistries` stay
 * reachable by URL without cluttering the menu.
 */

import { pathForPageKey } from "./paths.js";

export const NAV_GROUPS = [
  {
    id: "home",
    label: "Home",
    pageKeys: ["dashboard"],
  },
  {
    id: "operate",
    label: "Operate",
    pageKeys: [
      "clusters",
      "namespaces",
      "topology",
      "logs",
      "alerts",
      "upgrade",
      "clusterBuilder",
      "clusterManagement",
    ],
  },
  {
    id: "applications",
    label: "Applications",
    pageKeys: [
      "inventory",
      "applicationIntelligence",
      "applicationServices",
      "serviceCatalog",
      "components",
      "clients",
      "mobileApps",
    ],
  },
  {
    id: "changes",
    label: "Changes",
    pageKeys: ["deploymentRequests", "myRequests", "changeBundles", "ticketing"],
  },
  {
    id: "administration",
    label: "Administration",
    pageKeys: ["userManagement", "auditLogs", "settings"],
  },
];

/** Every pageKey that appears somewhere in the sidebar. */
export const GROUPED_PAGE_KEYS = new Set(NAV_GROUPS.flatMap((group) => group.pageKeys));

const GROUP_BY_PAGE_KEY = new Map(
  NAV_GROUPS.flatMap((group) => group.pageKeys.map((pageKey) => [pageKey, group.id]))
);

/**
 * The group whose menu should read as current.
 *
 * Takes the *nav* page key, so a drill-down has already been mapped to its
 * parent (`applicationDetails` -> `inventory`) and highlights Applications
 * rather than nothing.
 */
export function groupIdForPageKey(navPageKey) {
  return GROUP_BY_PAGE_KEY.get(navPageKey) || null;
}

/**
 * Build the sidebar from the pages this user may see.
 *
 * `visiblePages` is `getVisiblePages()` — already permission-filtered and
 * already excluding pages hidden by plan or config — so this adds grouping and
 * hrefs and makes no access decisions of its own. Empty groups are dropped: a
 * heading with nothing under it reads as something being broken.
 */
export function buildNavTree(visiblePages = []) {
  const byKey = new Map(visiblePages.map((page) => [page.key, page]));

  return NAV_GROUPS.map((group) => ({
    id: group.id,
    label: group.label,
    items: group.pageKeys
      .filter((pageKey) => byKey.has(pageKey))
      .map((pageKey) => ({
        pageKey,
        label: byKey.get(pageKey).label,
        href: pathForPageKey(pageKey),
      }))
      .filter((item) => item.href),
  })).filter((group) => group.items.length > 0);
}
