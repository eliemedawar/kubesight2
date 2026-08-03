import AccessDeniedPage from "../auth/AccessDenied.jsx";
import EmptyState from "./EmptyState.jsx";
import ErrorBanner from "./ErrorBanner.jsx";
import LoadingState from "./LoadingState.jsx";
import {
  ACCESS_VIEW,
  getScopeLoadingLabel,
  resolveAccessViewState,
  SCOPE_LOADING_HINT,
} from "../../utils/accessViewState.js";
import { EMPTY_MESSAGES } from "../../utils/authz.js";

/**
 * Gates page content behind loading / access-denied / error / degraded / empty
 * states. Never shows empty or denied UI while auth or scoped data is still
 * loading — that is the recurring bug it exists to prevent, an empty state
 * flashing before the first fetch resolves and reading as "there is nothing
 * here" when the honest answer is "we do not know yet".
 *
 * `degraded` is the one state that renders content *and* a warning: data
 * arrived but is stale or partial. Hiding usable-but-old data behind a banner
 * is as unhelpful as showing it as if it were fresh.
 */
export default function AccessScopeView({
  authLoading = false,
  coreLoading = false,
  pageLoading = false,
  namespacesLoading = false,
  resourcesLoading = false,
  accessError = "",
  empty = false,
  emptyMessage,
  emptyHint,
  emptyVariant,
  forceAccessDenied = false,
  degraded = false,
  degradedMessage = "",
  loadingLabel,
  loadingHint = SCOPE_LOADING_HINT,
  deniedMessage = EMPTY_MESSAGES.noAccess,
  children,
  header = null,
}) {
  const scopePageLoading = pageLoading || namespacesLoading || resourcesLoading;
  const viewState = resolveAccessViewState({
    authLoading,
    coreLoading,
    pageLoading: scopePageLoading,
    accessError,
    empty,
    forceAccessDenied,
  });
  const resolvedLoadingLabel =
    loadingLabel ||
    getScopeLoadingLabel({ coreLoading, namespacesLoading, resourcesLoading, pageLoading });

  return (
    <>
      {header}
      {viewState === ACCESS_VIEW.LOADING ? (
        <LoadingState label={resolvedLoadingLabel} hint={loadingHint} />
      ) : null}
      {viewState === ACCESS_VIEW.ACCESS_DENIED ? (
        <AccessDeniedPage message={deniedMessage} />
      ) : null}
      {viewState === ACCESS_VIEW.ERROR ? (
        <ErrorBanner message={accessError} suppressAccessDenied={false} />
      ) : null}
      {viewState === ACCESS_VIEW.EMPTY ? (
        <EmptyState message={emptyMessage} hint={emptyHint} variant={emptyVariant} />
      ) : null}
      {viewState === ACCESS_VIEW.LOADED && degraded ? (
        <p className="banner-message banner-message--warn" role="status">
          {degradedMessage || "Some of this may be out of date."}
        </p>
      ) : null}
      {viewState === ACCESS_VIEW.LOADED ? children : null}
    </>
  );
}
