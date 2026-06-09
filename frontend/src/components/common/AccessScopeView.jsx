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
 * Gates page content behind loading / access-denied / empty states.
 * Never shows empty or denied UI while auth or scoped data is still loading.
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
        <EmptyState message={emptyMessage} hint={emptyHint} />
      ) : null}
      {viewState === ACCESS_VIEW.LOADED ? children : null}
    </>
  );
}
