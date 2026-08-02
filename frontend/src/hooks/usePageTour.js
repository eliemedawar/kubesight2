import { useCallback, useEffect, useState } from "react";
import { getTourSteps, getWelcomeSteps, WELCOME_TOUR_KEY } from "../tours/tourDefinitions.js";
import { markTourSeen, readTourState, setToursMuted } from "../utils/tourStorage.js";

const AUTO_START_DELAY_MS = 800;

/**
 * Guided page tips.
 *
 * Section G step 7, and last out of App deliberately: the tour engine is keyed
 * by page key, so it depended on every route having a stable one. That is what
 * the route table now guarantees.
 *
 * Behaviour is unchanged. Each page's tour auto-runs once per user per browser
 * and can be replayed from the topbar. Steps are filtered with the same
 * permission predicates the pages render with, so nobody is shown a tip for a
 * control their role cannot see, and the engine additionally skips steps whose
 * target is not in the DOM.
 *
 * The one subtlety worth keeping visible: navigating away mid-tour marks it
 * seen. Without that, a tour interrupted by a click would re-run on every
 * subsequent visit, which is how a helpful thing becomes a nagging one.
 */
export function usePageTour({ pageKey, userId, enabled, isAdmin, hasPermission, pageAllowed }) {
  const [activeTour, setActiveTour] = useState(null);

  const buildSteps = useCallback(
    (targetPageKey) => {
      const ctx = { isAdmin, hasPermission, pageAllowed, pageKey: targetPageKey };
      const state = readTourState(userId);
      const pageSteps = getTourSteps(targetPageKey, ctx);
      const includesWelcome = !state.seen[WELCOME_TOUR_KEY];
      const steps =
        includesWelcome && pageSteps.length ? [...getWelcomeSteps(ctx), ...pageSteps] : pageSteps;
      return { steps, includesWelcome };
    },
    [isAdmin, hasPermission, pageAllowed, userId]
  );

  const markSeen = useCallback(
    (tour) => {
      if (!tour) {
        return;
      }
      markTourSeen(userId, tour.pageKey);
      if (tour.includesWelcome) {
        markTourSeen(userId, WELCOME_TOUR_KEY);
      }
    },
    [userId]
  );

  const start = useCallback(() => {
    if (!pageKey) {
      return;
    }
    const { steps, includesWelcome } = buildSteps(pageKey);
    if (steps.length) {
      setActiveTour({ pageKey, steps, auto: false, includesWelcome });
    }
  }, [pageKey, buildSteps]);

  const close = useCallback(() => {
    setActiveTour((current) => {
      markSeen(current);
      return null;
    });
  }, [markSeen]);

  const mute = useCallback(() => {
    setToursMuted(userId, true);
    close();
  }, [userId, close]);

  // Auto-run, once, after the page has had a moment to render its chrome so the
  // spotlight has targets to find. Slow data is handled by the engine's own
  // per-step polling.
  useEffect(() => {
    if (!enabled || !pageKey || activeTour) {
      return undefined;
    }
    const state = readTourState(userId);
    if (state.muted || state.seen[pageKey]) {
      return undefined;
    }
    const { steps, includesWelcome } = buildSteps(pageKey);
    if (!steps.length) {
      return undefined;
    }
    const timer = setTimeout(
      () => setActiveTour({ pageKey, steps, auto: true, includesWelcome }),
      AUTO_START_DELAY_MS
    );
    return () => clearTimeout(timer);
  }, [enabled, pageKey, activeTour, userId, buildSteps]);

  // Navigating away ends the tour and counts it as seen. Signing out clears it
  // without marking anything, so the next user starts fresh.
  useEffect(() => {
    if (!activeTour) {
      return;
    }
    if (!enabled) {
      setActiveTour(null);
      return;
    }
    if (activeTour.pageKey !== pageKey) {
      markSeen(activeTour);
      setActiveTour(null);
    }
  }, [activeTour, pageKey, enabled, markSeen]);

  return { activeTour, start, close, mute };
}
