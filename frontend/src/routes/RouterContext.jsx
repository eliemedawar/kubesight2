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
import {
  blockingGuards,
  discardGuards,
  hasUnloadGuards,
  historyDelta,
  historyIndex,
  historyState,
  leaveMessage,
} from "./leaveGuard.js";
import { askToLeave } from "./leavePrompt.js";

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
 *
 * Leave guards (routes/leaveGuard.js): before any move, the router asks the
 * pages with unsaved edits. `navigate` simply waits for the answer. Back and
 * Forward have already moved the address when we hear of them, so the router
 * steps back by the same distance (every entry it writes carries its history
 * index for that), asks, and only on "leave" steps forward again.
 */

const SETTLED_TRUE = Promise.resolve(true);
const SETTLED_FALSE = Promise.resolve(false);
// A traversal we started that never landed (history changed under it) must
// not leave the listener deaf for good.
const TRAVERSAL_TIMEOUT_MS = 1500;

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

  // Where the current entry sits in this tab's history. Seeded from the entry
  // itself, which keeps its state across a reload.
  const indexRef = useRef(null);
  if (indexRef.current === null) {
    indexRef.current =
      (typeof window !== "undefined" && historyIndex(window.history.state)) || 0;
  }
  // A leave question is open: further guarded moves wait for its answer.
  const promptingRef = useRef(false);
  // A history step we started ourselves: { hash, undo, done }.
  const traversalRef = useRef(null);

  /** Write the address; push moves the history index one forward. */
  const writeHistory = useCallback((hash, replace) => {
    if (typeof window === "undefined") {
      return;
    }
    const index = replace ? indexRef.current : indexRef.current + 1;
    const url = urlFor(hash);
    if (replace) {
      window.history.replaceState(historyState(index), "", url);
    } else {
      window.history.pushState(historyState(index), "", url);
    }
    indexRef.current = index;
  }, []);

  const navigate = useCallback((target, { replace = false, force = false } = {}) => {
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
      return SETTLED_TRUE;
    }

    const resolved = parseRoute(nextHash) || DEFAULT_ROUTE;
    const commit = () => {
      // Navigating to where we already are never stacks a history entry —
      // that is what stops a double-clicked nav item costing two Back presses.
      const stillUnchanged = buildRoute(routeRef.current) === nextHash;
      const useReplace = replace || stillUnchanged;
      writeHistory(nextHash, useReplace);
      if (!useReplace && typeof window !== "undefined") {
        // Arriving somewhere new starts at the top. On Back the browser
        // restores the previous scroll itself (scrollRestoration is "auto"),
        // which is why only the push branch scrolls.
        window.scrollTo(0, 0);
      }
      // Update the ref now, not on the next render: a handler that navigates
      // twice (switch tab, then set a query param) builds the second address
      // on this one instead of on the stale route, which silently dropped the
      // first.
      routeRef.current = resolved;
      setRoute(resolved);
    };

    const blocking = force || unchanged ? [] : blockingGuards(resolved, current);
    if (!blocking.length) {
      commit();
      return SETTLED_TRUE;
    }
    // One question at a time: a second guarded move while the first is open
    // is dropped rather than stacked behind it.
    if (promptingRef.current) {
      return SETTLED_FALSE;
    }
    promptingRef.current = true;
    return Promise.resolve(askToLeave(leaveMessage(blocking)))
      .catch(() => false)
      .then((leave) => {
        promptingRef.current = false;
        if (!leave) {
          return false;
        }
        discardGuards(blocking);
        commit();
        return true;
      });
  }, [writeHistory]);

  /** Navigate by bare page key — what the sidebar and cross-page links use. */
  const navigateToPage = useCallback(
    (pageKey, { params, query, replace = false } = {}) => {
      const row = routeForPageKey(pageKey);
      if (!row) {
        return SETTLED_FALSE;
      }
      return navigate({ key: row.key, params, query }, { replace });
    },
    [navigate]
  );

  const getRoute = useCallback(() => routeRef.current, []);

  // Back, forward, and hand-edited URLs. Our own writes never reach here.
  useEffect(() => {
    if (typeof window === "undefined") {
      return undefined;
    }
    let traversalTimer = null;
    // Step through history ourselves and recognise the arrival. `undo`
    // undoes a guarded Back/Forward; the redo after "leave" is not an undo.
    const traverse = (delta, hash, undo) =>
      new Promise((resolve) => {
        window.clearTimeout(traversalTimer);
        traversalRef.current = { hash, undo, done: resolve };
        traversalTimer = window.setTimeout(() => {
          if (traversalRef.current?.hash === hash) {
            traversalRef.current = null;
            resolve();
          }
        }, TRAVERSAL_TIMEOUT_MS);
        window.history.go(delta);
      });

    const apply = (parsed) => {
      const arrived = historyIndex(window.history.state);
      if (arrived === null) {
        // A link or a typed address made this entry: stamp it with its place.
        indexRef.current += 1;
        window.history.replaceState(historyState(indexRef.current), "", window.location.href);
      } else {
        indexRef.current = arrived;
      }
      routeRef.current = parsed;
      setRoute(parsed);
    };

    const sync = () => {
      const hash = readHash();
      const pending = traversalRef.current;
      if (pending) {
        // While our own step is in flight, the only event that matters is its
        // arrival; the hashchange twin of the move we just undid is ignored.
        if (hash !== pending.hash) {
          return;
        }
        traversalRef.current = null;
        window.clearTimeout(traversalTimer);
        if (pending.undo) {
          indexRef.current = historyIndex(window.history.state) ?? indexRef.current;
          pending.done();
          return;
        }
        pending.done();
        const parsed = parseRoute(hash);
        if (parsed && buildRoute(parsed) !== buildRoute(routeRef.current)) {
          apply(parsed);
        }
        return;
      }

      const parsed = parseRoute(hash);
      if (!parsed) {
        // Unrecognised address: replace it rather than render something that
        // did not come from the table, and do not leave it in history.
        const fallback = DEFAULT_ROUTE;
        window.history.replaceState(historyState(indexRef.current), "", urlFor(buildRoute(fallback)));
        routeRef.current = fallback;
        setRoute(fallback);
        return;
      }
      const canonical = buildRoute(parsed);
      const currentHash = buildRoute(routeRef.current);
      if (canonical === currentHash) {
        // Same destination, possibly spelled differently (#audit-logs for
        // #/audit-logs, or a default tab written out in full). Tidy the
        // address bar without adding a history entry, and do not re-render.
        if (hash !== canonical) {
          const arrived = historyIndex(window.history.state);
          window.history.replaceState(historyState(arrived ?? indexRef.current), "", urlFor(canonical));
        }
        return;
      }

      const blocking = blockingGuards(parsed, routeRef.current);
      if (!blocking.length) {
        apply(parsed);
        return;
      }

      // The browser has already moved. Put the address back where the page
      // still is, then ask; on "leave", step forward again.
      const delta = historyDelta(indexRef.current, historyIndex(window.history.state));
      const restored = delta
        ? traverse(-delta, currentHash, true)
        : Promise.resolve(window.history.replaceState(historyState(indexRef.current), "", urlFor(currentHash)));
      if (promptingRef.current) {
        // A question is already open (Back pressed again while it shows).
        return;
      }
      promptingRef.current = true;
      Promise.resolve(askToLeave(leaveMessage(blocking)))
        .catch(() => false)
        .then(async (leave) => {
          await restored;
          promptingRef.current = false;
          if (!leave) {
            return;
          }
          discardGuards(blocking);
          if (delta) {
            await traverse(delta, hash, false);
          } else {
            writeHistory(canonical, false);
            routeRef.current = parsed;
            setRoute(parsed);
          }
        });
    };
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      window.clearTimeout(traversalTimer);
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
  }, [writeHistory]);

  // Closing or reloading the tab with edits pending: the browser's own
  // question, the only one allowed there.
  useEffect(() => {
    if (typeof window === "undefined") {
      return undefined;
    }
    const warn = (event) => {
      if (!hasUnloadGuards()) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  // Canonicalise the address bar once: a bare "/" or a stale form becomes the
  // route we actually resolved to, without adding a history entry. Also
  // stamps the entry with its history index.
  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const canonical = buildRoute(routeRef.current);
    if (readHash() !== canonical || historyIndex(window.history.state) !== indexRef.current) {
      window.history.replaceState(historyState(indexRef.current), "", urlFor(canonical));
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
