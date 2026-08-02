import { useCallback, useEffect, useState } from "react";
import {
  getIntegration,
  listIntegrations,
  setIntegrationEnabled,
  testIntegration,
} from "../api/integrationsApi.js";

/**
 * Loading integrations, in one place.
 *
 * Contract 2 has two rules that are easy to break by accident from a component,
 * so they are enforced here instead:
 *
 *   Describing never tests. Every underlying `test_connection` writes
 *   `last_test_*` columns, so refreshing the hub on an interval would rewrite
 *   history just by looking at it — and make listing as slow as the slowest
 *   network round-trip. There is no polling in this file, deliberately.
 *
 *   Testing is slow by nature and must not time out under 30s. That is a
 *   pending state, not a spinner with a deadline.
 *
 * `error.status` distinguishes 404 (no such provider) from 403 (a provider this
 * user may not see). They read very differently to an operator and the client
 * already exposes the code.
 */

export function useIntegrationList() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await listIntegrations();
      setItems(response?.items || []);
    } catch (err) {
      setError(err.message || "Failed to load integrations.");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return { items, loading, error, reload: load };
}

export function useIntegration(providerKey) {
  const [integration, setIntegration] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notFound, setNotFound] = useState(false);
  const [forbidden, setForbidden] = useState(false);

  const load = useCallback(async () => {
    if (!providerKey) {
      return;
    }
    setLoading(true);
    setError("");
    setNotFound(false);
    setForbidden(false);
    try {
      setIntegration(await getIntegration(providerKey));
    } catch (err) {
      setIntegration(null);
      if (err.status === 404) {
        setNotFound(true);
      } else if (err.status === 403) {
        setForbidden(true);
      } else {
        setError(err.message || "Failed to load this integration.");
      }
    } finally {
      setLoading(false);
    }
  }, [providerKey]);

  useEffect(() => {
    load();
  }, [load]);

  return { integration, loading, error, notFound, forbidden, reload: load, setIntegration };
}

/**
 * Test and enable/disable.
 *
 * Both return the refreshed descriptor, so the caller replaces state rather
 * than guessing at the new status — the backend decides what a successful test
 * means for a provider that tracks sync separately from test.
 */
export function useIntegrationActions({ providerKey, onChanged }) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [toggling, setToggling] = useState(false);
  const [actionError, setActionError] = useState("");

  const runTest = useCallback(async () => {
    setTesting(true);
    setTestResult(null);
    setActionError("");
    try {
      const result = await testIntegration(providerKey);
      setTestResult({
        ok: result?.ok !== false,
        message:
          result?.message || (result?.ok === false ? "Test failed." : "Connection succeeded."),
      });
      await onChanged?.();
    } catch (err) {
      setTestResult({ ok: false, message: err.message || "Test failed." });
    } finally {
      setTesting(false);
    }
  }, [providerKey, onChanged]);

  const setEnabled = useCallback(
    async (enabled) => {
      setToggling(true);
      setActionError("");
      try {
        await setIntegrationEnabled(providerKey, enabled);
        await onChanged?.();
      } catch (err) {
        setActionError(err.message || "Could not change this setting.");
      } finally {
        setToggling(false);
      }
    },
    [providerKey, onChanged]
  );

  return { testing, testResult, toggling, actionError, runTest, setEnabled, setTestResult };
}
