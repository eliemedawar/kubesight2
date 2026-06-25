import { createContext, useCallback as useCb, useContext, useEffect, useMemo, useState } from "react";
import {
  addBundleItem,
  getMyDraftBundle,
  removeBundleItem,
  submitChangeBundle,
} from "../api/changeBundlesApi";
import { useAuth } from "./AuthContext";

const ChangeBundleContext = createContext(null);

/**
 * Holds the user's active *draft* Change Bundle (the "cart") and exposes helpers
 * so any screen can stage a change with one call, e.g.
 *   const { addItem, openDrawer } = useChangeBundle();
 *   addItem({ actionType: "scale_replicas", clusterId, namespace, resourceName, replicas });
 */
export function ChangeBundleProvider({ children }) {
  const { isAuthenticated, hasPermission } = useAuth();
  const canUse = isAuthenticated && hasPermission && hasPermission("change_bundles:create");

  const [bundle, setBundle] = useState(null);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCb(async () => {
    if (!canUse) {
      setBundle(null);
      return null;
    }
    setLoading(true);
    setError("");
    try {
      const data = await getMyDraftBundle();
      setBundle(data);
      return data;
    } catch (err) {
      setError(err.message || "Failed to load change bundle");
      return null;
    } finally {
      setLoading(false);
    }
  }, [canUse]);

  useEffect(() => {
    if (canUse) {
      refresh();
    } else {
      setBundle(null);
      setIsOpen(false);
    }
  }, [canUse, refresh]);

  const ensureBundle = useCb(async () => {
    if (bundle?.id) {
      return bundle;
    }
    return refresh();
  }, [bundle, refresh]);

  const addItem = useCb(
    async (descriptor) => {
      const current = await ensureBundle();
      if (!current?.id) {
        throw new Error("No active change bundle");
      }
      const updated = await addBundleItem(current.id, descriptor);
      setBundle(updated);
      return updated;
    },
    [ensureBundle]
  );

  const removeItem = useCb(
    async (itemId) => {
      if (!bundle?.id) return null;
      const updated = await removeBundleItem(bundle.id, itemId);
      setBundle(updated);
      return updated;
    },
    [bundle]
  );

  const submit = useCb(
    async (payload) => {
      if (!bundle?.id) {
        throw new Error("No active change bundle");
      }
      const result = await submitChangeBundle(bundle.id, payload);
      // Submitting closes out the draft; pull a fresh (new) draft.
      await refresh();
      setIsOpen(false);
      return result;
    },
    [bundle, refresh]
  );

  const value = useMemo(
    () => ({
      enabled: canUse,
      bundle,
      itemCount: bundle?.items?.length || 0,
      loading,
      error,
      isOpen,
      openDrawer: () => setIsOpen(true),
      closeDrawer: () => setIsOpen(false),
      refresh,
      addItem,
      removeItem,
      submit,
      addAndOpen: async (descriptor) => {
        const updated = await addItem(descriptor);
        setIsOpen(true);
        return updated;
      },
    }),
    [canUse, bundle, loading, error, isOpen, refresh, addItem, removeItem, submit]
  );

  return <ChangeBundleContext.Provider value={value}>{children}</ChangeBundleContext.Provider>;
}

export function useChangeBundle() {
  const ctx = useContext(ChangeBundleContext);
  if (!ctx) {
    // Safe no-op fallback so components used outside the provider don't crash.
    return {
      enabled: false,
      bundle: null,
      itemCount: 0,
      loading: false,
      error: "",
      isOpen: false,
      openDrawer: () => {},
      closeDrawer: () => {},
      refresh: async () => null,
      addItem: async () => {
        throw new Error("Change bundles are not available");
      },
      removeItem: async () => null,
      submit: async () => {
        throw new Error("Change bundles are not available");
      },
      addAndOpen: async () => {
        throw new Error("Change bundles are not available");
      },
    };
  }
  return ctx;
}
