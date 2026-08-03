import { useEffect, useMemo, useState } from "react";
import { listIntegrations } from "../api/integrationsApi.js";
import { listDeploymentRequests } from "../api/deploymentRequestsApi.js";

/**
 * The extra sources the attention feed needs beyond the dashboard summary.
 *
 * Two rules shape this:
 *
 *   Only fetch on the dashboard. These are feed inputs, not app-wide state, and
 *   loading integrations on every page to populate a list nobody is looking at
 *   is exactly the kind of cost that made the old per-page effects expensive.
 *
 *   Only fetch what this user may see. Each source is gated on the permission
 *   its own page is gated on, so the feed never provokes a 403 to discover that
 *   a user is not an approver.
 *
 * A source that fails reports its name rather than resolving to an empty list.
 * The feed being short because integrations did not load is a different fact
 * from nothing being wrong, and the operator has to be able to tell.
 */
export function useAttentionSources({ enabled, canViewIntegrations, canViewApprovals }) {
  const [integrations, setIntegrations] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [failed, setFailed] = useState([]);

  const wantIntegrations = Boolean(enabled && canViewIntegrations);
  const wantApprovals = Boolean(enabled && canViewApprovals);

  useEffect(() => {
    if (!wantIntegrations) {
      setIntegrations([]);
      return undefined;
    }
    let cancelled = false;
    listIntegrations()
      .then((response) => {
        if (!cancelled) {
          setIntegrations(response?.items || []);
          setFailed((current) => current.filter((name) => name !== "integrations"));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setIntegrations([]);
          setFailed((current) =>
            current.includes("integrations") ? current : [...current, "integrations"]
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [wantIntegrations]);

  useEffect(() => {
    if (!wantApprovals) {
      setApprovals([]);
      return undefined;
    }
    let cancelled = false;
    listDeploymentRequests({ limit: 100 })
      .then((response) => {
        if (!cancelled) {
          setApprovals(response?.items || []);
          setFailed((current) => current.filter((name) => name !== "approvals"));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setApprovals([]);
          setFailed((current) =>
            current.includes("approvals") ? current : [...current, "approvals"]
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [wantApprovals]);

  // Only name a source as unavailable if it was one we actually tried. A user
  // who is not an approver is not missing approvals data; there is none for
  // them, which is not a degraded state.
  const unavailable = useMemo(
    () =>
      failed.filter(
        (name) =>
          (name === "integrations" && wantIntegrations) || (name === "approvals" && wantApprovals)
      ),
    [failed, wantIntegrations, wantApprovals]
  );

  return { integrations, approvals, unavailable };
}
