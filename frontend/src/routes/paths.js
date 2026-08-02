/**
 * pageKey <-> URL, in both directions.
 *
 * Both directions delegate to React Router's own `matchRoutes` / `generatePath`
 * rather than reimplementing matching. That is deliberate: the shell derives
 * `activePage` from the URL while `<Routes>` independently decides what to
 * render, and if those two used different matchers they would eventually
 * disagree — the chrome would say one page and the body would show another.
 * One ranking algorithm, used twice.
 */

import { generatePath, matchRoutes } from "react-router-dom";
import { ROUTES, routeForPageKey } from "./routeTable.js";

// matchRoutes wants route objects; carry the pageKey through so a match can be
// mapped back to a table row.
const MATCHABLE = ROUTES.map((route) => ({ path: route.path, pageKey: route.pageKey }));

/**
 * The route table row for a URL, or null if nothing matches (a 404).
 * Returns `{ pageKey, params }` — params are the decoded path parameters.
 */
export function matchPath(pathname) {
  const matches = matchRoutes(MATCHABLE, pathname || "/");
  if (!matches?.length) {
    return null;
  }
  // Last match is the most specific; these routes are flat, so it is the only one.
  const match = matches[matches.length - 1];
  return { pageKey: match.route.pageKey, params: match.params || {} };
}

/** The pageKey a URL resolves to, or "" for no match. */
export function pageKeyForPath(pathname) {
  return matchPath(pathname)?.pageKey || "";
}

/**
 * The URL for a pageKey.
 *
 * Throws if a required param is missing, which is what we want: a nav entry
 * pointing at `/applications/:applicationId` with no id is a bug at the call
 * site, and failing here names the pageKey instead of silently producing a
 * path with a literal ":applicationId" in it.
 */
export function pathForPageKey(pageKey, params = {}) {
  const route = routeForPageKey(pageKey);
  if (!route) {
    return null;
  }
  try {
    return generatePath(route.path, params);
  } catch (error) {
    throw new Error(
      `Cannot build a URL for page "${pageKey}" (${route.path}): ${error.message}`
    );
  }
}

/** True when the path has a route. Used to tell a 404 from an access denial. */
export function isKnownPath(pathname) {
  return matchPath(pathname) !== null;
}
