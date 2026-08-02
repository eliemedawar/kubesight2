import { useCallback, useEffect, useRef, useState } from "react";
import {
  getUpgradeInfo,
  getUpgradeJob,
  runUpgradePrecheck,
  startUpgrade,
} from "../api/upgradesApi.js";
import { mapPrecheckState } from "../utils/formatters.js";
import { formatAccessError, shouldShowAccessError } from "../utils/authz.js";

/** The version asked for before the backend tells us what it recommends. */
const INITIAL_TARGET = "v1.31.0";
const JOB_POLL_MS = 3000;

/**
 * Flatten the several shapes the upgrade endpoints return into one.
 *
 * Exported for its own tests: precheck, start and job-status responses each
 * carry a different subset of these fields, and the merge rules below are the
 * only thing keeping the page from showing a half-updated plan.
 */
export function normalizeUpgradePayload(payload) {
  return {
    clusterInfo: payload.clusterInfo,
    provider: payload.provider,
    versionInfo: payload.versionInfo,
    versionSkew: payload.versionSkew,
    upgradePlan: payload.upgradePlan,
    instructions: payload.instructions || payload.provider?.instructions,
    currentVersion: payload.currentVersion || payload.clusterInfo?.controlPlaneVersion,
    canUpgrade: payload.canUpgrade,
    status: payload.status,
    message: payload.message,
    upgradeId: payload.upgradeId,
    executionSupported: payload.executionSupported,
    requiredConfirmation: payload.requiredConfirmation,
    upgradeChecks: (payload.checks || []).map((check) => ({
      item: check.name,
      state: mapPrecheckState(check.status),
      rawStatus: check.status,
      message: check.details || check.message,
    })),
    upgradeSteps: payload.steps || payload.upgradePlan?.steps || [],
    activeStep: payload.activeStep ?? -1,
  };
}

/** True when this provider can only show instructions, never execute. */
export function isInstructionsOnly(upgrade) {
  const mode = upgrade?.provider?.executionMode;
  if (mode === "instructions") {
    return true;
  }
  return (
    !upgrade?.provider?.upgradeSupported && mode !== "plan-only" && mode !== "execute-with-cli"
  );
}

/** True when starting will actually drain nodes rather than print a plan. */
export function willExecuteAutomatically(upgrade) {
  return upgrade?.provider?.executionMode === "execute-with-cli";
}

/**
 * The Upgrade Center's data and actions.
 *
 * Audit items E3 (load on arrival) and E4 (3s job poll). The poll's original
 * comment said "keep polling until the job completes or the user leaves the
 * page" — unmounting is exactly that condition, so owning it here expresses the
 * intent the page-key guard was approximating.
 *
 * Starting an upgrade that drains nodes used to go through `window.confirm`.
 * That is the highest blast radius confirmation in the product and the one
 * least able to explain itself: no list of what happens, no way to distinguish
 * it from an ordinary "are you sure", and dismissed by the same Enter that got
 * the operator there. It is a `ConfirmDialog` now, which also means this hook
 * has no DOM dependency and can be tested.
 */
export function useUpgradeCenter({ clusterId, clusterLabel, canAccessCluster }) {
  const [upgrade, setUpgrade] = useState(null);
  const [targetVersion, setTargetVersion] = useState(INITIAL_TARGET);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [confirmingStart, setConfirmingStart] = useState(false);

  // Which cluster the in-flight request belongs to, so a switch mid-flight does
  // not paint one cluster's plan under another's name.
  const loadClusterRef = useRef("");

  const reportError = useCallback((message) => {
    if (shouldShowAccessError(message)) {
      setError(formatAccessError(message));
    }
  }, []);

  const load = useCallback(
    async (targetClusterId, version, { resetTarget = false } = {}) => {
      if (!targetClusterId || !canAccessCluster?.(targetClusterId)) {
        return;
      }
      loadClusterRef.current = targetClusterId;
      setLoading(true);
      setError("");
      try {
        const result = await getUpgradeInfo(targetClusterId, version);
        if (loadClusterRef.current !== targetClusterId) {
          return;
        }
        setUpgrade({ ...normalizeUpgradePayload(result), clusterId: targetClusterId });
        if (resetTarget) {
          // Prefer what the backend recommends over the version we guessed at.
          const recommended = result.versionInfo?.recommendedTarget;
          const latest = result.versionInfo?.latestAvailable;
          if (recommended) {
            setTargetVersion(recommended);
          } else if (latest && latest !== "unknown") {
            setTargetVersion(latest);
          } else {
            setTargetVersion(version);
          }
        }
      } catch (loadError) {
        if (loadClusterRef.current === targetClusterId) {
          reportError(loadError.message);
        }
      } finally {
        if (loadClusterRef.current === targetClusterId) {
          setLoading(false);
        }
      }
    },
    [canAccessCluster, reportError]
  );

  // E3: arriving, or changing cluster, starts from a clean plan.
  useEffect(() => {
    if (!clusterId) {
      return;
    }
    setUpgrade(null);
    setTargetVersion(INITIAL_TARGET);
    load(clusterId, INITIAL_TARGET, { resetTarget: true });
  }, [clusterId, load]);

  // E4: follow a running job. Unmounting stops it.
  const jobId = upgrade?.upgradeId || upgrade?.jobId;
  const isRunning = upgrade?.status === "running";
  useEffect(() => {
    if (!isRunning || !jobId) {
      return undefined;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const job = await getUpgradeJob(jobId);
        if (cancelled) {
          return;
        }
        setUpgrade((prev) => ({
          ...prev,
          status: job.status,
          message: job.message,
          error: job.error,
          upgradeId: job.jobId || prev?.upgradeId,
          upgradeSteps: job.steps || prev?.upgradeSteps,
          activeStep: job.activeStep ?? prev?.activeStep ?? -1,
          executionSupported: job.executionSupported ?? prev?.executionSupported,
        }));
      } catch {
        // A failed poll is not a failed job. Keep asking until it finishes or
        // the operator leaves.
      }
    };
    poll();
    const timer = setInterval(poll, JOB_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [isRunning, jobId]);

  const changeTargetVersion = useCallback(
    (version) => {
      setTargetVersion(version);
      if (clusterId) {
        load(clusterId, version);
      }
    },
    [clusterId, load]
  );

  const runPrecheck = useCallback(async () => {
    if (!clusterId || !canAccessCluster?.(clusterId)) {
      return;
    }
    setLoading(true);
    setError("");
    try {
      const result = await runUpgradePrecheck({ clusterId, targetVersion });
      setUpgrade({
        ...normalizeUpgradePayload(result),
        clusterId,
        canUpgrade: Boolean(result.canUpgrade),
        status: result.canUpgrade ? null : "blocked",
        message: result.canUpgrade
          ? `Precheck passed. Current version: ${result.currentVersion || "unknown"}`
          : "Precheck failed. Fix failed checks before upgrading.",
      });
    } catch (precheckError) {
      reportError(precheckError.message);
    } finally {
      setLoading(false);
    }
  }, [clusterId, targetVersion, canAccessCluster, reportError]);

  const performStart = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await startUpgrade({ clusterId, targetVersion });
      const normalized = normalizeUpgradePayload(result);
      const completedIndex = (normalized.upgradeSteps || []).reduce(
        (last, step, index) => (step.status === "completed" ? index : last),
        -1
      );
      setUpgrade((prev) => ({
        ...normalized,
        clusterId,
        canUpgrade: prev?.canUpgrade ?? true,
        status: result.status,
        message: result.message,
        activeStep: result.activeStep ?? completedIndex,
        upgradeSteps: result.steps || normalized.upgradeSteps,
        executionSupported: result.executionSupported ?? normalized.executionSupported,
        jobId: result.jobId || result.upgradeId,
      }));
      return result;
    } catch (startError) {
      reportError(startError.message);
      return null;
    } finally {
      setLoading(false);
    }
  }, [clusterId, targetVersion, reportError]);

  /**
   * Ask to start. Returns "instructions" when the provider only documents the
   * steps, "blocked" when a precheck has failed, "confirm" when the caller must
   * resolve the dialog first, and "started" when the request went out.
   */
  const requestStart = useCallback(async () => {
    if (!clusterId || !canAccessCluster?.(clusterId)) {
      return "denied";
    }
    if (isInstructionsOnly(upgrade)) {
      return "instructions";
    }
    if (upgrade && upgrade.canUpgrade === false) {
      setError("Run a successful precheck before starting upgrade.");
      return "blocked";
    }
    if (willExecuteAutomatically(upgrade)) {
      setConfirmingStart(true);
      return "confirm";
    }
    await performStart();
    return "started";
  }, [clusterId, canAccessCluster, upgrade, performStart]);

  const confirmStart = useCallback(async () => {
    setConfirmingStart(false);
    return performStart();
  }, [performStart]);

  const cancelStart = useCallback(() => setConfirmingStart(false), []);

  return {
    upgrade,
    targetVersion,
    loading,
    error,
    confirmingStart,
    confirmationLabel: clusterLabel || clusterId || "",
    changeTargetVersion,
    runPrecheck,
    requestStart,
    confirmStart,
    cancelStart,
  };
}
