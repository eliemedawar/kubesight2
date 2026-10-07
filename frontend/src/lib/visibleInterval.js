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
