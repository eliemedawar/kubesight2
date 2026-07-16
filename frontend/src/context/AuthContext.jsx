import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import {
  changeTemporaryPassword as apiChangePassword,
  fetchCurrentUser,
  login as apiLogin,
  logout as apiLogout,
  setupTotp as apiSetupTotp,
  setStoredSession,
  verifyFirstLoginTotp as apiVerifyFirstLoginTotp,
  verifyLoginMfa as apiVerifyLoginMfa,
} from "../api/authApi";

import { setUnauthorizedHandler } from "../api/client";

import { clearStoredSession, getStoredToken, getStoredUser } from "../authStorage";

import { createAuthAccess, hasPermission as checkPermission } from "../lib/authAccess";

const AuthContext = createContext(null);

// Stages returned by the login state machine that require an interim screen
// (before a full session exists).
const ONBOARDING_STAGES = new Set(["password_change", "mfa_setup", "mfa"]);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(getStoredUser);
  const [token, setToken] = useState(getStoredToken);
  const [loading, setLoading] = useState(Boolean(getStoredToken()));
  const [error, setError] = useState("");
  // Interim login state: the multi-step onboarding / MFA challenge that runs
  // between submitting credentials and receiving a full session. `null` once the
  // user is fully authenticated (or before any login attempt).
  const [pendingAuth, setPendingAuth] = useState(null);
  // Structured lock info from a 423 response ({kind, reason, retryAfterSeconds}).
  // Lives here (not in LoginPage) because the page remounts around the global
  // loading splash and would lose local state before it could render the scene.
  const [accountLock, setAccountLock] = useState(null);

  // Returns true when the error is an account lock; switches the auth UI to the
  // lock scene and abandons any interim (onboarding / MFA) session.
  const captureLock = useCallback((err) => {
    if (err?.status === 423 && err.data?.lock) {
      setAccountLock(err.data.lock);
      setPendingAuth(null);
      setError("");
      return true;
    }
    return false;
  }, []);

  const clearAccountLock = useCallback(() => {
    setAccountLock(null);
    setError("");
  }, []);

  const refreshUser = useCallback(async () => {
    const currentToken = getStoredToken();
    if (!currentToken) {
      setUser(null);
      setToken("");
      setLoading(false);
      return null;
    }
    try {
      const profile = await fetchCurrentUser();
      setUser(profile);
      setStoredSession(currentToken, profile);
      setToken(currentToken);
      return profile;
    } catch (err) {
      clearStoredSession();
      setUser(null);
      setToken("");
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      clearStoredSession();
      setUser(null);
      setToken("");
    });
    if (getStoredToken()) {
      refreshUser().catch(() => {});
    } else {
      setLoading(false);
    }
  }, [refreshUser]);

  const finalizeAuth = useCallback((data) => {
    setStoredSession(data.token, data.user);
    setUser(data.user);
    setToken(data.token);
    setPendingAuth(null);
    setError("");
    return data.user;
  }, []);

  // Submit username + password. Either logs the user straight in, or transitions
  // to an interim stage (first-login onboarding or an MFA challenge).
  const login = async (username, password) => {
    setError("");
    setLoading(true);
    try {
      const data = await apiLogin(username, password);
      if (data.stage === "authenticated") {
        return { stage: "authenticated", user: finalizeAuth(data) };
      }
      setPendingAuth({ ...data, username: data.username || username });
      return data;
    } catch (err) {
      if (!captureLock(err)) {
        setError(err.message || "Login failed");
      }
      throw err;
    } finally {
      setLoading(false);
    }
  };

  // First-login step 1: replace the temporary password. Advances the stage.
  const submitPasswordChange = async (newPassword) => {
    if (!pendingAuth?.onboardingToken) {
      throw new Error("Onboarding session expired. Please sign in again.");
    }
    const data = await apiChangePassword(pendingAuth.onboardingToken, newPassword);
    setPendingAuth((prev) => ({ ...prev, stage: data.stage, mfaEnabled: data.mfaEnabled }));
    return data;
  };

  // First-login step 2a: fetch a fresh TOTP secret + QR to enroll an authenticator.
  const startTotpSetup = async () => {
    if (!pendingAuth?.onboardingToken) {
      throw new Error("Onboarding session expired. Please sign in again.");
    }
    return apiSetupTotp(pendingAuth.onboardingToken);
  };

  // First-login step 2b: verify the enrollment code; completes onboarding.
  const submitFirstLoginTotp = async (code) => {
    if (!pendingAuth?.onboardingToken) {
      throw new Error("Onboarding session expired. Please sign in again.");
    }
    try {
      const data = await apiVerifyFirstLoginTotp(pendingAuth.onboardingToken, code);
      return { stage: "authenticated", user: finalizeAuth(data) };
    } catch (err) {
      captureLock(err);
      throw err;
    }
  };

  // Normal-login MFA challenge: verify the 6-digit code and complete sign-in.
  const submitLoginMfa = async (code) => {
    if (!pendingAuth?.mfaToken) {
      throw new Error("MFA session expired. Please sign in again.");
    }
    try {
      const data = await apiVerifyLoginMfa(pendingAuth.mfaToken, code);
      return { stage: "authenticated", user: finalizeAuth(data) };
    } catch (err) {
      // A lock kills the interim MFA token; the client must restart login.
      captureLock(err);
      throw err;
    }
  };

  const cancelPendingAuth = useCallback(() => {
    setPendingAuth(null);
    setError("");
  }, []);

  const logout = async () => {
    try {
      await apiLogout();
    } catch {
      // ignore
    }
    clearStoredSession();
    setUser(null);
    setToken("");
    setPendingAuth(null);
  };

  const hasPermission = useCallback(
    (permission) => checkPermission(user, permission),
    [user]
  );

  const access = useMemo(() => createAuthAccess(user), [user]);

  const value = useMemo(
    () => ({
      user,
      token,
      loading,
      error,
      setError,
      login,
      logout,
      refreshUser,
      hasPermission,
      isAuthenticated: Boolean(user && token),
      // Onboarding / MFA flow
      pendingAuth,
      authStage: pendingAuth?.stage || (user && token ? "authenticated" : "login"),
      needsOnboarding: Boolean(pendingAuth && ONBOARDING_STAGES.has(pendingAuth.stage)),
      needsMfaChallenge: pendingAuth?.stage === "mfa_challenge",
      submitPasswordChange,
      startTotpSetup,
      submitFirstLoginTotp,
      submitLoginMfa,
      cancelPendingAuth,
      accountLock,
      clearAccountLock,
      isAdmin: access.isAdmin,
      hasAnyPermission: access.hasAnyPermission,
      canAccessCluster: access.canAccessCluster,
      canAccessNamespace: access.canAccessNamespace,
      canAccessResource: access.canAccessResource,
      canViewLogs: access.canViewLogs,
      canViewAlert: access.canViewAlert,
      filterAlertsForUser: access.filterAlertsForUser,
      canViewServicePort: access.canViewServicePort,
      getAllowedClusters: access.getAllowedClusters,
      getAllowedNamespaces: access.getAllowedNamespaces,
      getAllowedResources: access.getAllowedResources,
      getLogVisiblePods: access.getLogVisiblePods,
      hasAnyClusterAccess: access.hasAnyClusterAccess,
      hasAnyNamespaceAccess: access.hasAnyNamespaceAccess,
      canAccessResourcesPage: access.canAccessResourcesPage,
      canAccessLogsPage: access.canAccessLogsPage,
      canViewResourceTab: access.canViewResourceTab,
      getVisibleResourceTabs: access.getVisibleResourceTabs,
      pageNeedsClusterContext: access.pageNeedsClusterContext,
      pageNeedsNamespaceContext: access.pageNeedsNamespaceContext,
      pageAllowed: access.pageAllowed,
      getVisiblePages: access.getVisiblePages,
      getFirstAllowedPage: access.getFirstAllowedPage,
      isAccessDeniedError: access.isAccessDeniedError,
      formatAccessError: access.formatAccessError,
      shouldShowAccessError: access.shouldShowAccessError,
    }),
    [user, token, loading, error, pendingAuth, accountLock, clearAccountLock, refreshUser, hasPermission, access]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
};
