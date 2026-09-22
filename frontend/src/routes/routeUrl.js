/**
 * The only module that knows KubeSight uses hash routing.
 *
 * Hash rather than history paths because vite builds with `base: "./"` and
 * `backend/api/frontend_static.py` serves only `/` — a path route would 404 on
 * refresh and resolve `./assets/...` against the wrong directory. The fragment
 * also never reaches the server, so cluster and namespace names stay out of
 * access logs and out of the `Referer` header.
 *
 * Switching to history paths later means rewriting `parseRoute`/`buildRoute`
 * here, flipping vite's `base`, and adding a Flask catch-all. Nothing else in
 * the app reads `window.location`.
 *
 * Pure: no React, no DOM. Everything here is unit-tested in a node env.
 */

import {
  DEFAULT_ROUTE_KEY,
  ROUTES,
  patternSegments,
  routeByKey,
  routeForPageKey,
  slugMapFor,
  slugifyValue,
} from "./routeTable.js";

/** Strip the leading "#" and any leading "/" noise, keep the query. */
function stripHash(hash) {
  let value = String(hash || "");
  if (value.startsWith("#")) {
    value = value.slice(1);
  }
  if (!value.startsWith("/")) {
    value = `/${value}`;
  }
  return value;
}

function splitPathAndQuery(value) {
  const index = value.indexOf("?");
  if (index === -1) {
    return [value, ""];
  }
  return [value.slice(0, index), value.slice(index + 1)];
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    // A malformed escape is a malformed URL, not a crash.
    return value;
  }
}

/**
 * Only the query keys the route declares survive. This is the open-redirect
 * and query-injection guard: an undeclared `?next=` never reaches the app.
 */
function parseQuery(search, allowed) {
  const out = {};
  if (!search || !allowed?.length) {
    return out;
  }
  const permitted = new Set(allowed);
  search.split("&").forEach((pair) => {
    if (!pair) {
      return;
    }
    const eq = pair.indexOf("=");
    const rawKey = eq === -1 ? pair : pair.slice(0, eq);
    const rawValue = eq === -1 ? "" : pair.slice(eq + 1);
    const key = safeDecode(rawKey);
    if (!permitted.has(key)) {
      return;
    }
    const value = safeDecode(rawValue.replace(/\+/g, " "));
    if (value !== "") {
      out[key] = value;
    }
  });
  return out;
}

/**
 * Try one route row against the path segments.
 * Returns its params, or null when the row does not match — including when an
 * enumerated param carries a value outside its vocabulary, which is what keeps
 * `/inventory/app` from matching `/inventory/:section`.
 */
function matchRoute(route, segments) {
  const pattern = patternSegments(route.path);
  if (segments.length > pattern.length) {
    return null;
  }
  const params = {};
  for (let index = 0; index < pattern.length; index += 1) {
    const slot = pattern[index];
    const raw = segments[index];

    if (raw === undefined) {
      // Ran out of input: fine only if everything left is optional.
      if (!slot.optional) {
        return null;
      }
      continue;
    }

    if (slot.literal !== undefined) {
      if (slot.literal !== raw) {
        return null;
      }
      continue;
    }

    const decoded = safeDecode(raw);
    if (decoded === "") {
      if (!slot.optional) {
        return null;
      }
      continue;
    }

    const descriptor = route.params?.[slot.name];
    const slugs = slugMapFor(descriptor);
    if (slugs) {
      const resolved = slugs.get(slugifyValue(decoded));
      if (resolved === undefined) {
        return null;
      }
      params[slot.name] = resolved;
    } else {
      params[slot.name] = decoded;
    }
  }
  return params;
}

/**
 * Parse a location hash into a route.
 *
 * Returns null for anything unrecognised — an unknown page, an unknown tab, a
 * malformed path. Callers redirect to the default route rather than rendering
 * a value that did not come from the table.
 */
export function parseRoute(hash) {
  const [path, search] = splitPathAndQuery(stripHash(hash));
  const segments = path.split("/").filter(Boolean);

  if (!segments.length) {
    return null;
  }

  for (const route of ROUTES) {
    const params = matchRoute(route, segments);
    if (params) {
      return {
        key: route.key,
        params: { ...route.defaults, ...params },
        query: parseQuery(search, route.query),
      };
    }
  }
  return null;
}

/**
 * Serialise a route back to a hash, including the leading "#".
 *
 * Params equal to their default are dropped from the tail so the URL stays
 * canonical: buildRoute is idempotent through parseRoute.
 */
export function buildRoute(target) {
  const route = routeByKey(target?.key) || routeByKey(DEFAULT_ROUTE_KEY);
  const params = { ...route.defaults, ...(target?.params || {}) };
  const pattern = patternSegments(route.path);

  const parts = [];
  pattern.forEach((slot) => {
    if (slot.literal !== undefined) {
      parts.push({ text: slot.literal, droppable: false });
      return;
    }
    const value = params[slot.name];
    if (value === undefined || value === null || value === "") {
      parts.push({ text: "", droppable: true, missing: true });
      return;
    }
    const descriptor = route.params?.[slot.name];
    const text = descriptor?.values
      ? slugifyValue(value)
      : encodeURIComponent(String(value));
    const isDefault = route.defaults?.[slot.name] === value;
    parts.push({ text, droppable: Boolean(slot.optional) && isDefault });
  });

  // Drop trailing segments that are defaults or absent; a gap in the middle
  // means the route was built with missing required params, so stop there.
  while (parts.length && parts[parts.length - 1].droppable) {
    parts.pop();
  }
  const firstMissing = parts.findIndex((part) => part.missing);
  const usable = firstMissing === -1 ? parts : parts.slice(0, firstMissing);

  let out = `#/${usable.map((part) => part.text).join("/")}`;
  if (out.endsWith("/") && out.length > 2) {
    out = out.slice(0, -1);
  }

  const query = target?.query || {};
  const pairs = (route.query || [])
    .filter((key) => {
      const value = query[key];
      return value !== undefined && value !== null && value !== "";
    })
    .map((key) => `${encodeURIComponent(key)}=${encodeURIComponent(String(query[key]))}`);

  return pairs.length ? `${out}?${pairs.join("&")}` : out;
}

/** Convenience: the hash for a bare page key, with optional params/query. */
export function hashForPage(pageKey, { params, query } = {}) {
  const route = routeForPageKey(pageKey) || routeByKey(DEFAULT_ROUTE_KEY);
  return buildRoute({ key: route.key, params, query });
}

/** True when two routes address the same view — the echo guard in useRouter. */
export function sameRoute(a, b) {
  if (!a || !b) {
    return a === b;
  }
  return buildRoute(a) === buildRoute(b);
}

export const DEFAULT_ROUTE = { key: DEFAULT_ROUTE_KEY, params: {}, query: {} };
