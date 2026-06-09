import { describe, expect, it } from "vitest";
import {
  ACCESS_VIEW,
  getScopeLoadingLabel,
  isAuthOrDataLoading,
  isClusterScopeLoading,
  isNamespaceScopeLoading,
  pageNeedsResourceData,
  resolveAccessViewState,
  shouldDeferAccessMessage,
} from "./accessViewState.js";
import { EMPTY_MESSAGES, formatAccessError, isAccessDeniedError } from "./authz.js";

describe("resolveAccessViewState", () => {
  it("returns loading while auth or data fetches are active", () => {
    expect(resolveAccessViewState({ authLoading: true, empty: true })).toBe(ACCESS_VIEW.LOADING);
    expect(resolveAccessViewState({ coreLoading: true, empty: true })).toBe(ACCESS_VIEW.LOADING);
    expect(resolveAccessViewState({ pageLoading: true, empty: true })).toBe(ACCESS_VIEW.LOADING);
  });

  it("returns empty only after loading completes", () => {
    expect(resolveAccessViewState({ empty: true })).toBe(ACCESS_VIEW.EMPTY);
  });

  it("returns accessDenied for API 403-style messages after loading", () => {
    expect(
      resolveAccessViewState({
        accessError: "You do not have access to this resource.",
      })
    ).toBe(ACCESS_VIEW.ACCESS_DENIED);
  });

  it("returns error for non-access failures after loading", () => {
    expect(resolveAccessViewState({ accessError: "Network error" })).toBe(ACCESS_VIEW.ERROR);
  });

  it("returns loaded when scope is ready", () => {
    expect(resolveAccessViewState({})).toBe(ACCESS_VIEW.LOADED);
  });
});

describe("loading helpers", () => {
  it("detects auth or data loading", () => {
    expect(isAuthOrDataLoading({ coreLoading: true })).toBe(true);
    expect(isAuthOrDataLoading({})).toBe(false);
  });

  it("scopes cluster vs namespace loading", () => {
    expect(isClusterScopeLoading({ coreLoading: true })).toBe(true);
    expect(isNamespaceScopeLoading({ pageLoading: true })).toBe(true);
    expect(isClusterScopeLoading({ pageLoading: true })).toBe(false);
  });

  it("defers access messages while loading", () => {
    expect(shouldDeferAccessMessage({ pageLoading: true })).toBe(true);
    expect(shouldDeferAccessMessage({})).toBe(false);
  });
});

describe("scope loading labels", () => {
  it("prioritizes resources over namespaces in loading copy", () => {
    expect(
      getScopeLoadingLabel({ namespacesLoading: true, resourcesLoading: true })
    ).toBe("Loading resources...");
    expect(getScopeLoadingLabel({ namespacesLoading: true })).toBe("Loading namespaces...");
  });

  it("knows which pages need resource payloads", () => {
    expect(pageNeedsResourceData("resources")).toBe(true);
    expect(pageNeedsResourceData("namespaces")).toBe(false);
  });
});

describe("API access denied messages", () => {
  it("detects 403-style errors for post-load display", () => {
    expect(isAccessDeniedError("You do not have access to this resource.")).toBe(true);
    expect(isAccessDeniedError(EMPTY_MESSAGES.unexpectedAccess)).toBe(true);
  });

  it("formats API denials as the user-facing noAccess message", () => {
    expect(formatAccessError("Forbidden")).toBe(EMPTY_MESSAGES.noAccess);
  });
});
