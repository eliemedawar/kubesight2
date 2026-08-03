/**
 * Breadcrumbs, derived from the route table rather than declared per page.
 *
 * The `parent` field already records that an application detail belongs under
 * Applications and a cluster overview under Clusters — that is what keeps the
 * right nav entry highlighted. A page that also hand-wrote its own trail would
 * be a second copy of the same fact, free to disagree with the first.
 *
 * A trail is: the nav group, then the parent chain, then the page itself. Only
 * the middle is navigable — the group is a heading, not a destination, and the
 * last crumb is where you already are.
 */

import { NAV_PAGES } from "../utils/authz.js";
import { pathForPageKey } from "./paths.js";
import { groupIdForPageKey, NAV_GROUPS } from "./navigation.js";
import { navPageKeyFor, routeForPageKey } from "./routeTable.js";

const LABEL_BY_PAGE_KEY = new Map(NAV_PAGES.map((page) => [page.key, page.label]));
const GROUP_LABEL_BY_ID = new Map(NAV_GROUPS.map((group) => [group.id, group.label]));

/** Fallback label for drill-downs, which are not NAV_PAGES entries. */
const DRILL_DOWN_LABELS = {
  clusterOverview: "Cluster",
  applicationDetails: "Application",
  resources: "Resources",
};

export function labelForPageKey(pageKey) {
  return LABEL_BY_PAGE_KEY.get(pageKey) || DRILL_DOWN_LABELS[pageKey] || pageKey || "";
}

/**
 * The trail for a page.
 *
 * `params` are the current route params, used to build hrefs for ancestors that
 * take them. `currentLabel` overrides the last crumb — a detail page knows the
 * application is called "payments-api" and the route table never can.
 *
 * Ancestors whose href cannot be built (a missing param) are dropped rather
 * than rendered as dead text: a crumb that looks like a link and is not is
 * worse than one crumb fewer.
 */
export function breadcrumbsFor(pageKey, { params = {}, currentLabel } = {}) {
  const route = routeForPageKey(pageKey);
  if (!route) {
    return [];
  }

  const chain = [];
  let cursor = route;
  const seen = new Set();
  while (cursor && !seen.has(cursor.pageKey)) {
    seen.add(cursor.pageKey);
    chain.unshift(cursor);
    cursor = cursor.parent ? routeForPageKey(cursor.parent) : null;
  }

  const crumbs = [];

  const groupId = groupIdForPageKey(navPageKeyFor(pageKey));
  if (groupId) {
    crumbs.push({ label: GROUP_LABEL_BY_ID.get(groupId), href: null, isGroup: true });
  }

  chain.forEach((entry, index) => {
    const isCurrent = index === chain.length - 1;
    const label = isCurrent ? currentLabel || labelForPageKey(entry.pageKey) : labelForPageKey(entry.pageKey);

    if (isCurrent) {
      crumbs.push({ label, href: null, isCurrent: true, pageKey: entry.pageKey });
      return;
    }

    let href = null;
    try {
      href = pathForPageKey(entry.pageKey, params);
    } catch {
      href = null;
    }
    if (href) {
      crumbs.push({ label, href, pageKey: entry.pageKey });
    }
  });

  return crumbs;
}
