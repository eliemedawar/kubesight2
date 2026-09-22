import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { DEFAULT_ROUTE, buildRoute, hashForPage, parseRoute } from "./routeUrl.js";
import { navKeyOf, pageKeyOf, routeByKey, routeForPageKey } from "./routeTable.js";

/**
 * Hash router.
 *
 * The URL is the single source of truth for which page, which tab, and which
 * record is open. Nothing else in the app keeps a parallel copy — that is what
 * makes Back work without a desync.
 *
 * The one trap this is built around: `history.pushState` and
 * `history.replaceState` do NOT fire `hashchange` or `popstate`. So
 * `navigate()` writes history AND sets React state itself, and the listener
 * exists only for user-driven back/forward and manual address-bar edits. Both
 * paths compare the serialised address first and no-op when it is unchanged,
 * which is the echo guard.
 */

const RouterContext = createContext(null);

function readHash() {
  if (typeof window === "undefined") {
    return "";
  }
  return window.location.hash || "";
}

function urlFor(hash) {
  if (typeof window === "undefined") {
    return hash;
  }
  const { pathname, search } = window.location;
  return `${pathname}${search}${hash}`;
}

export function RouterProvider({ children }) {
  const [route, setRoute] = useState(() => parseRoute(readHash()) || DEFAULT_ROUTE);

  // Read inside stable callbacks without making them depend on the route, so
  // `navigate` keeps one identity for the life of the app.
  const routeRef = useRef(route);
  routeRef.current = route;

  const navigate = useCallback((target, { replace = false } = {}) => {
    const key = target?.key || routeRef.current?.key || DEFAULT_ROUTE.key;
    const next = {
      key,
      params: target?.params || {},
      query: target?.query || {},
    };
    const nextHash = buildRoute(next);
    const current = routeRef.current;
    const unchanged = current && buildRoute(current) === nextHash;

    if (unchanged && readHash() === nextHash) {
      return;
    }

    // Navigating to where we already are never stacks a history entry — that
    // is what stops a double-clicked nav item costing two Back presses.
    const useReplace = replace || unchanged;
    if (typeof window !== "undefined") {
      const url = urlFor(nextHash);
      if (useReplace) {
        window.history.replaceState(null, "", url);
      } else {
        window.history.pushState(null, "", url);
        // Arriving somewhere new starts at the top. On Back the browser
        // restores the previous scroll itself (scrollRestoration is "auto"),
        // which is why only the push branch scrolls.
        window.scrollTo(0, 0);
      }
    }
    setRoute(parseRoute(nextHash) || DEFAULT_ROUTE);
  }, []);

  /** Navigate by bare page key — what the sidebar and cross-page links use. */
  const navigateToPage = useCallback(
    (pageKey, { params, query, replace = false } = {}) => {
      const row = routeForPageKey(pageKey);
      if (!row) {
        return;
      }
      navigate({ key: row.key, params, query }, { replace });
    },
    [navigate]
  );

  const getRoute = useCallback(() => routeRef.current, []);

  // Back, forward, and hand-edited URLs. Our own writes never reach here.
  useEffect(() => {
    if (typeof window === "undefined") {
      return undefined;
    }
    const sync = () => {
      const parsed = parseRoute(readHash());
      if (!parsed) {
        // Unrecognised address: replace it rather than render something that
        // did not come from the table, and do not leave it in history.
        const fallback = DEFAULT_ROUTE;
        window.history.replaceState(null, "", urlFor(buildRoute(fallback)));
        setRoute(fallback);
        return;
      }
      const canonical = buildRoute(parsed);
      if (canonical === buildRoute(routeRef.current)) {
        // Same destination, possibly spelled differently (#audit-logs for
        // #/audit-logs, or a default tab written out in full). Tidy the
        // address bar without adding a history entry, and do not re-render.
        if (readHash() !== canonical) {
          window.history.replaceState(null, "", urlFor(canonical));
        }
        return;
      }
      setRoute(parsed);
    };
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
  }, []);

  // Canonicalise the address bar once: a bare "/" or a stale form becomes the
  // route we actually resolved to, without adding a history entry.
  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const canonical = buildRoute(routeRef.current);
    if (readHash() !== canonical) {
      window.history.replaceState(null, "", urlFor(canonical));
    }
    // Runs on every route change: cheap, and keeps the bar honest after a
    // default-filling navigation.
  }, [route]);

  const value = useMemo(() => {
    const row = routeByKey(route.key);
    return {
      route,
      getRoute,
      navigate,
      navigateToPage,
      // The authz + renderPage key. Sub-routes resolve to the page that owns them.
      pageKey: row ? pageKeyOf(row) : DEFAULT_ROUTE.key,
      // Which sidebar entry highlights.
      navKey: row ? navKeyOf(row) : DEFAULT_ROUTE.key,
      params: route.params || {},
      query: route.query || {},
      // The query keys this address is allowed to carry. App uses it to decide
      // whether the scope selectors belong in the URL on this page.
      queryKeys: row?.query || [],
    };
  }, [route, getRoute, navigate, navigateToPage]);

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useRouter() {
  const context = useContext(RouterContext);
  if (!context) {
    throw new Error("useRouter must be used inside <RouterProvider>");
  }
  return context;
}

/**
 * A path param, with the same signature as useState.
 *
 *   const [tab, setTab] = useRouteParam("tab", "overview");
 *
 * Writes push by default: a tab change is something Back should undo.
 */
export function useRouteParam(name, fallback, { replace = false } = {}) {
  const { route, getRoute, navigate } = useRouter();
  const current = route.params?.[name] ?? fallback;

  const set = useCallback(
    (next) => {
      const active = getRoute();
      const previous = active.params?.[name] ?? fallback;
      const resolved = typeof next === "function" ? next(previous) : next;
      navigate(
        {
          key: active.key,
          params: { ...active.params, [name]: resolved },
          query: active.query,
        },
        { replace }
      );
    },
    [getRoute, navigate, name, fallback, replace]
  );

  return [current, set];
}

/**
 * A query param, same signature. Writes replace by default: query values are
 * usually a refinement of where you already are, not a new place.
 */
export function useRouteQuery(name, fallback = null, { replace = true } = {}) {
  const { route, getRoute, navigate } = useRouter();
  const current = route.query?.[name] ?? fallback;

  const set = useCallback(
    (next) => {
      const active = getRoute();
      const previous = active.query?.[name] ?? fallback;
      const resolved = typeof next === "function" ? next(previous) : next;
      const query = { ...active.query };
      if (resolved === null || resolved === undefined || resolved === "") {
        delete query[name];
      } else {
        query[name] = resolved;
      }
      navigate({ key: active.key, params: active.params, query }, { replace });
    },
    [getRoute, navigate, name, fallback, replace]
  );

  return [current, set];
}

/**
 * A list/detail pair of routes as one useState-shaped value.
 *
 *   const [openId, setOpenId] = useEntityRoute("clients", "clientDetail", "clientId");
 *
 * Setting an id opens the detail address; setting null returns to the list.
 * The value is always a string (it came from a URL), so compare with String()
 * on both sides — record ids are usually numbers.
 */
export function useEntityRoute(listKey, detailKey, paramName) {
  const { route, getRoute, navigate } = useRouter();
  const current = route.key === detailKey ? route.params?.[paramName] ?? null : null;

  const set = useCallback(
    (next, { replace = false } = {}) => {
      const active = getRoute();
      const previous = active.key === detailKey ? active.params?.[paramName] ?? null : null;
      const resolved = typeof next === "function" ? next(previous) : next;
      if (resolved === null || resolved === undefined || resolved === "") {
        navigate({ key: listKey, params: {}, query: active.query }, { replace });
        return;
      }
      navigate(
        {
          key: detailKey,
          params: { ...active.params, [paramName]: String(resolved) },
          query: active.query,
        },
        { replace }
      );
    },
    [getRoute, navigate, listKey, detailKey, paramName]
  );

  return [current, set];
}

/** href for a page key, for real anchors in the sidebar. */
export function pageHref(pageKey, options) {
  return hashForPage(pageKey, options);
}
