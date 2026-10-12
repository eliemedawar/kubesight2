/**
 * The "waiting on me" approvals count, shared by the Sidebar badge and the
 * Approvals page.
 *
 * The Approvals inbox polls every 20 s and already knows the count, so while
 * it is on screen it claims the count (`claimApprovalsCount`) and publishes
 * every number it loads; the Sidebar then skips its own 60 s fetch. When no
 * inbox holds a claim the Sidebar fetches for itself, as before. A vote
 * publishes at once (`adjustApprovalsWaiting(-1)`), so the badge never lags
 * a decision the person just made.
 */
import { useSyncExternalStore } from "react";

let state = { value: null, at: 0 };
let claims = 0;
const listeners = new Set();

function emit() {
  listeners.forEach((listener) => listener());
}

/** Publish a fresh count (from /api/approvals/count or the inbox's counts). */
export function setApprovalsWaiting(value) {
  const next = Number(value);
  if (!Number.isFinite(next)) return;
  state = { value: Math.max(0, next), at: Date.now() };
  emit();
}

/** Shift the count right away (e.g. -1 after a vote), before the reload lands. */
export function adjustApprovalsWaiting(delta) {
  if (state.value == null) return;
  setApprovalsWaiting(state.value + delta);
}

/** The last published count, or null before anything was loaded. */
export function getApprovalsWaiting() {
  return state.value;
}

/** How old the last published count is, in ms (Infinity before the first). */
export function approvalsWaitingAge(now = Date.now()) {
  return state.at ? now - state.at : Infinity;
}

/** While an inbox is polling, it owns the count. Returns the release function. */
export function claimApprovalsCount() {
  claims += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    claims = Math.max(0, claims - 1);
  };
}

export function approvalsCountClaimed() {
  return claims > 0;
}

export function subscribeApprovalsWaiting(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** React: the current count (null until something was loaded). */
export function useApprovalsWaitingValue() {
  return useSyncExternalStore(subscribeApprovalsWaiting, getApprovalsWaiting, getApprovalsWaiting);
}

/** Tests only. */
export function resetApprovalsBadge() {
  state = { value: null, at: 0 };
  claims = 0;
  listeners.clear();
}
