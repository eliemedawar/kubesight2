import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import {

  fetchCurrentUser,

  login as apiLogin,

  logout as apiLogout,

  setUnauthorizedHandler,

} from "../api";

import { clearStoredSession, getStoredToken, getStoredUser, setStoredSession } from "../authStorage";

import { createAuthAccess, hasPermission as checkPermission } from "../lib/authAccess";



const AuthContext = createContext(null);



export function AuthProvider({ children }) {

  const [user, setUser] = useState(getStoredUser);

  const [token, setToken] = useState(getStoredToken);

  const [loading, setLoading] = useState(Boolean(getStoredToken()));

  const [error, setError] = useState("");



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



  const login = async (username, password) => {

    setError("");

    setLoading(true);

    try {

      const data = await apiLogin(username, password);

      setUser(data.user);

      setToken(data.token);

      return data.user;

    } catch (err) {

      setError(err.message || "Login failed");

      throw err;

    } finally {

      setLoading(false);

    }

  };



  const logout = async () => {

    try {

      await apiLogout();

    } catch {

      // ignore

    }

    clearStoredSession();

    setUser(null);

    setToken("");

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

    [user, token, loading, error, refreshUser, hasPermission, access]

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

