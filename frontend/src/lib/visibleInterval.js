/**
 * setInterval for data polling that pauses while the browser tab is hidden.
 *
 * KubeSight tabs stay open all day; every hidden tab used to keep polling the
 * API (and through it the clusters) at the same rate as the one being looked
 * at. While hidden, ticks are skipped; when the tab becomes visible again and
 * a tick was missed, `fn` runs at once so the page is current when looked at.
 *
 * Returns a cleanup function (call it where you would call clearInterval).
 */
export function setVisibleInterval(fn, intervalMs) {
  let lastRun = Date.now();
  let missed = false;

  const isHidden = () => typeof document !== "undefined" && document.hidden;

  const run = () => {
    lastRun = Date.now();
    missed = false;
    fn();
  };

  const timer = setInterval(() => {
    if (isHidden()) {
      missed = true;
      return;
    }
    run();
  }, intervalMs);

  const onVisibilityChange = () => {
    if (isHidden()) {
      return;
    }
    if (missed || Date.now() - lastRun >= intervalMs) {
      run();
    }
  };

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  return () => {
    clearInterval(timer);
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibilityChange);
    }
  };
}

/**
 * A promise that resolves once the tab is visible — at once if it already is.
 *
 * For polling loops built from chained setTimeouts rather than an interval:
 * `await whenVisible()` at the top of each tick, and a hidden tab stops
 * asking the API until somebody looks at it again, then catches up at once.
 * (Check your own "cancelled" flag after the await: the component may have
 * unmounted while the tab was hidden.)
 */
export function whenVisible() {
  if (typeof document === "undefined" || !document.hidden) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const onVisibilityChange = () => {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      resolve();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
  });
}
