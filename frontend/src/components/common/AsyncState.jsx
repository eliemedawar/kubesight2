import EmptyState from "./EmptyState.jsx";
import LoadingState from "./LoadingState.jsx";
import { ACCESS_VIEW, resolveAccessViewState } from "../../utils/accessViewState.js";

/**
 * One place that decides which of loading / denied / error / degraded / empty /
 * content a page is showing.
 *
 * Pages were each re-deriving this, and getting the precedence subtly
 * different — the recurring bug being an empty state that flashes before the
 * first fetch resolves, which reads as "there is nothing here" when the honest
 * answer is "we do not know yet". `resolveAccessViewState` already encodes the
 * rule that loading wins; this is the rendering half of it.
 *
 * Degraded is the state the audit found missing everywhere: data arrived, but
 * it is stale or partial. It renders the content *and* says so, because hiding
 * usable-but-old data behind a banner is as unhelpful as showing it as if it
 * were fresh.
 */
export default function AsyncState({
  loading = false,
  authLoading = false,
  coreLoading = false,
  error = "",
  forceAccessDenied = false,
  empty = false,
  degraded = false,
  degradedMessage = "",
  loadingLabel,
  loadingHint,
  emptyMessage = "Nothing here yet",
  emptyHint = "",
  emptyVariant,
  deniedMessage = "You do not have access to this resource.",
  children,
}) {
  const view = resolveAccessViewState({
    authLoading,
    coreLoading,
    pageLoading: loading,
    accessError: error,
    empty,
    forceAccessDenied,
  });

  if (view === ACCESS_VIEW.LOADING) {
    return <LoadingState label={loadingLabel} hint={loadingHint} />;
  }

  if (view === ACCESS_VIEW.ACCESS_DENIED) {
    return (
      <section className="card access-denied" role="status">
        <h3>Access restricted</h3>
        <p className="muted">{deniedMessage}</p>
      </section>
    );
  }

  if (view === ACCESS_VIEW.ERROR) {
    return (
      <section className="card state-error" role="alert">
        <h3>Could not load this</h3>
        <p className="muted">{error}</p>
      </section>
    );
  }

  if (view === ACCESS_VIEW.EMPTY) {
    return <EmptyState message={emptyMessage} hint={emptyHint} variant={emptyVariant} />;
  }

  if (degraded) {
    return (
      <>
        <p className="banner-message banner-message--warn" role="status">
          {degradedMessage || "Some of this may be out of date."}
        </p>
        {children}
      </>
    );
  }

  return children;
}
