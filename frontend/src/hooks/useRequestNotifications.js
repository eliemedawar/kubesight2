import { useCallback, useEffect, useMemo, useState } from "react";
import { listMyDeploymentRequests } from "../api/deploymentRequestsApi.js";

const POLL_MS = 30_000;

/** Identifies a decision, not a request: re-deciding is a new thing to notify about. */
const signatureOf = (request) => `${request.id}:${request.decidedAt || ""}`;

/**
 * Per-user, per-browser storage. Namespaced by user id so a shared machine does
 * not carry one person's read state into the next person's bell — the ticketing
 * provider key, deleted in the previous change, was the one place that got this
 * wrong.
 */
function readSet(key) {
  if (!key) {
    return new Set();
  }
  try {
    const raw = window.localStorage.getItem(key);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function writeSet(key, value) {
  if (!key) {
    return;
  }
  try {
    window.localStorage.setItem(key, JSON.stringify([...value]));
  } catch {
    // Private mode, or storage full. The badge simply reappears next load,
    // which is the right way to fail: it over-notifies rather than hiding
    // a decision the user has not actually seen.
  }
}

/**
 * Decided deployment requests, for the notifications bell.
 *
 * Extracted from App unchanged in behaviour. It was already page-independent —
 * its own comment said so — which is why it is the one piece of App state that
 * needed extracting rather than rehoming: nothing about it belonged to a route,
 * and it only sat in App because that is where everything sat.
 *
 * Two separate storage keys, because "seen" and "dismissed" answer different
 * questions. Seen clears the count but leaves the item in the list; dismissed
 * removes it. Collapsing them would mean opening the bell threw away the list.
 */
export function useRequestNotifications({ enabled, userId }) {
  const [items, setItems] = useState([]);

  const seenKey = userId ? `kubesight.seenRequestUpdates.${userId}` : null;
  const dismissedKey = userId ? `kubesight.dismissedRequestUpdates.${userId}` : null;

  const [seen, setSeen] = useState(() => readSet(seenKey));
  const [dismissed, setDismissed] = useState(() => readSet(dismissedKey));

  useEffect(() => setSeen(readSet(seenKey)), [seenKey]);
  useEffect(() => setDismissed(readSet(dismissedKey)), [dismissedKey]);

  useEffect(() => {
    if (!enabled) {
      setItems([]);
      return undefined;
    }
    let cancelled = false;

    const load = async () => {
      try {
        const res = await listMyDeploymentRequests({ limit: 100 });
        if (cancelled) {
          return;
        }
        const decided = (res.items || [])
          .filter((row) => row.status && row.status !== "pending")
          .sort(
            (a, b) => new Date(b.decidedAt || b.createdAt) - new Date(a.decidedAt || a.createdAt)
          );
        setItems(decided);
      } catch {
        if (!cancelled) {
          setItems([]);
        }
      }
    };

    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [enabled, userId]);

  const visible = useMemo(
    () => items.filter((request) => !dismissed.has(signatureOf(request))),
    [items, dismissed]
  );

  const unseenCount = useMemo(
    () => visible.filter((request) => !seen.has(signatureOf(request))).length,
    [visible, seen]
  );

  const markAllSeen = useCallback(() => {
    if (!seenKey || !visible.length) {
      return;
    }
    setSeen((prev) => {
      const next = new Set(prev);
      visible.forEach((request) => next.add(signatureOf(request)));
      writeSet(seenKey, next);
      return next;
    });
  }, [seenKey, visible]);

  const dismiss = useCallback(
    (request) => {
      if (!dismissedKey || !request) {
        return;
      }
      setDismissed((prev) => {
        const next = new Set(prev);
        next.add(signatureOf(request));
        writeSet(dismissedKey, next);
        return next;
      });
    },
    [dismissedKey]
  );

  const dismissAll = useCallback(() => {
    if (!dismissedKey || !visible.length) {
      return;
    }
    setDismissed((prev) => {
      const next = new Set(prev);
      visible.forEach((request) => next.add(signatureOf(request)));
      writeSet(dismissedKey, next);
      return next;
    });
  }, [dismissedKey, visible]);

  return { items: visible, unseenCount, markAllSeen, dismiss, dismissAll };
}
