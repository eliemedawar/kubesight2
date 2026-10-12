/**
 * Leave guards: "this page has edits that are not saved yet".
 *
 * A page registers a guard while it is dirty (see hooks/useUnsavedChanges.js).
 * The router asks the registry before it moves anywhere — `navigate`, browser
 * Back/Forward, a typed address — and the window asks it before the tab closes
 * or reloads. This module is the pure part: no React, no DOM, so the decision
 * rules can be tested on their own.
 *
 * A guard is a plain object:
 *   { message, blocks?(next, current), onDiscard?(), unload? }
 * `blocks` decides whether a given move loses the draft; without it (or when it
 * returns undefined), a move to
 * another page or another path param (a tab, a record) does, and a query-only
 * move (a drawer, a filter) does not.
 */

import { pageKeyOf, routeByKey } from "./routeTable.js";

const DEFAULT_MESSAGE = "You have changes that are not saved. Leave and discard them?";

const guards = new Map();
let sequence = 0;

/** Add a guard; returns the function that removes it again. */
export function registerGuard(guard) {
  sequence += 1;
  const id = sequence;
  guards.set(id, guard);
  return () => {
    guards.delete(id);
  };
}

/** Remove every guard. For tests, and for sign-out. */
export function clearGuards() {
  guards.clear();
}

export function guardCount() {
  return guards.size;
}

function sameParams(a = {}, b = {}) {
  const left = Object.entries(a || {}).filter(([, value]) => value !== undefined && value !== null && value !== "");
  const right = Object.entries(b || {}).filter(([, value]) => value !== undefined && value !== null && value !== "");
  if (left.length !== right.length) {
    return false;
  }
  return left.every(([key, value]) => String(b[key]) === String(value));
}

/**
 * Whether moving from `current` to `next` leaves the place a draft lives.
 * Another route key or another path param does; a query-only change is a
 * refinement of the same place (the router treats it the same way: replace,
 * not push).
 */
export function leavesRoute(next, current) {
  if (!next || !current) {
    return true;
  }
  return next.key !== current.key || !sameParams(next.params, current.params);
}

/**
 * Whether the move lands on another page altogether. For editors whose draft
 * survives moves inside the page (the Cluster Builder wizard keeps its draft
 * across that page's tabs) and is lost only when the page unmounts.
 */
export function leavesPage(next, current) {
  if (!next || !current) {
    return true;
  }
  const pageOf = (route) => {
    const row = routeByKey(route.key);
    return row ? pageKeyOf(row) : route.key;
  };
  return pageOf(next) !== pageOf(current);
}

/** The guards this particular move would trip, newest first. */
export function blockingGuards(next, current) {
  const hits = [];
  for (const [id, guard] of guards) {
    let blocks;
    try {
      // A predicate with no opinion (undefined) gets the default rule.
      const verdict = guard.blocks ? guard.blocks(next, current) : undefined;
      blocks = verdict === undefined ? leavesRoute(next, current) : Boolean(verdict);
    } catch {
      // A broken predicate must not trap the user on the page.
      blocks = false;
    }
    if (blocks) {
      hits.push({ id, guard });
    }
  }
  return hits.reverse();
}

/** The one question to ask for a set of blocking guards. */
export function leaveMessage(blocking) {
  const messages = [...new Set(blocking.map(({ guard }) => guard.message).filter(Boolean))];
  if (!messages.length) {
    return DEFAULT_MESSAGE;
  }
  if (messages.length === 1) {
    return messages[0];
  }
  // Two editors dirty at once (a settings draft plus an MCP panel, say):
  // the newest one speaks, and the count says it is not alone.
  return `${messages[0]} (${messages.length - 1} other unsaved change${messages.length > 2 ? "s" : ""} will be lost too.)`;
}

/**
 * The user chose to leave: drop the guards that asked and let each page
 * forget its draft. Removed before the move commits, so a page that is still
 * mounted for a moment (its effects run after the route changes) cannot ask
 * a second time.
 */
export function discardGuards(blocking) {
  for (const { id, guard } of blocking) {
    guards.delete(id);
    try {
      guard.onDiscard?.();
    } catch {
      // The navigation goes ahead regardless.
    }
  }
}

/**
 * The guards signing out would lose: the same ones closing the tab asks about.
 * A draft kept across reloads (`unload: false`) survives a sign-out too.
 */
export function exitGuards() {
  const hits = [];
  for (const [id, guard] of guards) {
    if (guard.unload !== false) {
      hits.push({ id, guard });
    }
  }
  return hits.reverse();
}

/** Whether closing or reloading the tab should be questioned. */
export function hasUnloadGuards() {
  for (const guard of guards.values()) {
    if (guard.unload !== false) {
      return true;
    }
  }
  return false;
}

// --- History positions --------------------------------------------------------
//
// A Back/Forward press has already changed the address by the time the page
// hears about it. To keep the user where they were, the router has to step
// back the same distance, so every history entry it writes carries its index.

export const HISTORY_INDEX_KEY = "ksIdx";

export function historyIndex(state) {
  const value = state?.[HISTORY_INDEX_KEY];
  return Number.isInteger(value) ? value : null;
}

export function historyState(index) {
  return { [HISTORY_INDEX_KEY]: index };
}

/**
 * How far the browser moved: positive for Forward, negative for Back. An
 * entry with no index was just created by a link or a typed address, so it
 * sits one past the entry we were on.
 */
export function historyDelta(currentIndex, arrivedIndex) {
  if (arrivedIndex === null || arrivedIndex === undefined || currentIndex === null || currentIndex === undefined) {
    return 1;
  }
  return arrivedIndex - currentIndex;
}
