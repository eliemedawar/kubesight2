// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getUpgradeInfo = vi.fn();
const getUpgradeJob = vi.fn();
const runUpgradePrecheck = vi.fn();
const startUpgrade = vi.fn();

vi.mock("../api/upgradesApi.js", () => ({
  getUpgradeInfo: (...a) => getUpgradeInfo(...a),
  getUpgradeJob: (...a) => getUpgradeJob(...a),
  runUpgradePrecheck: (...a) => runUpgradePrecheck(...a),
  startUpgrade: (...a) => startUpgrade(...a),
}));

const { useUpgradeCenter, isInstructionsOnly, willExecuteAutomatically, normalizeUpgradePayload } =
  await import("./useUpgradeCenter.js");

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
beforeEach(() => {
  vi.clearAllMocks();
  getUpgradeInfo.mockResolvedValue({ provider: {}, versionInfo: {} });
});

let latest = null;

function Probe(props) {
  latest = useUpgradeCenter(props);
  return null;
}

const allowAll = () => true;
const renderHook = (props = {}) =>
  render(<Probe clusterId="prod-eu" clusterLabel="Production EU" canAccessCluster={allowAll} {...props} />);

const executeProvider = { executionMode: "execute-with-cli", upgradeSupported: true };

describe("starting an upgrade that drains nodes", () => {
  // The whole reason this moved off window.confirm: the destructive path must
  // be gated, and the gate must be inspectable.
  it("asks for confirmation before sending the request", async () => {
    getUpgradeInfo.mockResolvedValue({ provider: executeProvider, canUpgrade: true });
    renderHook();
    await waitFor(() => expect(latest.upgrade).toBeTruthy());

    let outcome;
    await act(async () => {
      outcome = await latest.requestStart();
    });

    expect(outcome).toBe("confirm");
    expect(latest.confirmingStart).toBe(true);
    expect(startUpgrade).not.toHaveBeenCalled();
  });

  it("sends the request only once confirmed", async () => {
    getUpgradeInfo.mockResolvedValue({ provider: executeProvider, canUpgrade: true });
    startUpgrade.mockResolvedValue({ status: "running", jobId: "j1" });
    getUpgradeJob.mockResolvedValue({ status: "running" });
    renderHook();
    await waitFor(() => expect(latest.upgrade).toBeTruthy());

    await act(async () => {
      await latest.requestStart();
    });
    await act(async () => {
      await latest.confirmStart();
    });

    expect(startUpgrade).toHaveBeenCalledTimes(1);
    expect(latest.confirmingStart).toBe(false);
  });

  it("sends nothing when cancelled", async () => {
    getUpgradeInfo.mockResolvedValue({ provider: executeProvider, canUpgrade: true });
    renderHook();
    await waitFor(() => expect(latest.upgrade).toBeTruthy());

    await act(async () => {
      await latest.requestStart();
    });
    act(() => latest.cancelStart());

    expect(latest.confirmingStart).toBe(false);
    expect(startUpgrade).not.toHaveBeenCalled();
  });

  // A plan-only provider changes nothing on the cluster, so gating it would be
  // friction that teaches operators to click through the gate that matters.
  it("does not gate a provider that only generates a plan", async () => {
    getUpgradeInfo.mockResolvedValue({
      provider: { executionMode: "plan-only", upgradeSupported: true },
      canUpgrade: true,
    });
    startUpgrade.mockResolvedValue({ status: "manual_required" });
    renderHook();
    await waitFor(() => expect(latest.upgrade).toBeTruthy());

    let outcome;
    await act(async () => {
      outcome = await latest.requestStart();
    });

    expect(outcome).toBe("started");
    expect(latest.confirmingStart).toBe(false);
    expect(startUpgrade).toHaveBeenCalled();
  });
});

describe("refusing to start", () => {
  it("refuses after a failed precheck and says why", async () => {
    getUpgradeInfo.mockResolvedValue({ provider: executeProvider, canUpgrade: false });
    renderHook();
    await waitFor(() => expect(latest.upgrade).toBeTruthy());

    let outcome;
    await act(async () => {
      outcome = await latest.requestStart();
    });

    expect(outcome).toBe("blocked");
    expect(latest.error).toMatch(/precheck/i);
    expect(startUpgrade).not.toHaveBeenCalled();
  });

  it("redirects to instructions for a provider that cannot execute", async () => {
    getUpgradeInfo.mockResolvedValue({
      provider: { executionMode: "instructions" },
      canUpgrade: true,
    });
    renderHook();
    await waitFor(() => expect(latest.upgrade).toBeTruthy());

    let outcome;
    await act(async () => {
      outcome = await latest.requestStart();
    });

    expect(outcome).toBe("instructions");
    expect(startUpgrade).not.toHaveBeenCalled();
  });

  it("refuses a cluster the user cannot access", async () => {
    renderHook({ canAccessCluster: () => false });
    let outcome;
    await act(async () => {
      outcome = await latest.requestStart();
    });
    expect(outcome).toBe("denied");
    expect(startUpgrade).not.toHaveBeenCalled();
  });
});

describe("job polling", () => {
  it("follows a running job and stops when it finishes", async () => {
    vi.useFakeTimers();
    getUpgradeInfo.mockResolvedValue({ provider: executeProvider, status: "running", upgradeId: "j1" });
    getUpgradeJob.mockResolvedValue({ status: "running" });
    renderHook();
    await act(async () => {});

    const before = getUpgradeJob.mock.calls.length;
    expect(before).toBeGreaterThan(0);

    // advanceTimersByTimeAsync, not waitFor: waitFor polls on real timers,
    // which are faked here, so it would wait forever.
    getUpgradeJob.mockResolvedValue({ status: "completed" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(latest.upgrade.status).toBe("completed");

    const afterCompletion = getUpgradeJob.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(getUpgradeJob.mock.calls.length).toBe(afterCompletion);
  });

  it("stops polling when the route unmounts", async () => {
    vi.useFakeTimers();
    getUpgradeInfo.mockResolvedValue({ provider: executeProvider, status: "running", upgradeId: "j1" });
    getUpgradeJob.mockResolvedValue({ status: "running" });
    const { unmount } = renderHook();
    await act(async () => {});
    const before = getUpgradeJob.mock.calls.length;

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(getUpgradeJob.mock.calls.length).toBe(before);
  });

  // A failed poll is not a failed job; the network can drop without the upgrade
  // stopping, and giving up would strand the operator with a stale status.
  it("keeps polling through a failed request", async () => {
    vi.useFakeTimers();
    getUpgradeInfo.mockResolvedValue({ provider: executeProvider, status: "running", upgradeId: "j1" });
    getUpgradeJob.mockRejectedValue(new Error("network"));
    renderHook();
    await act(async () => {});
    const before = getUpgradeJob.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(getUpgradeJob.mock.calls.length).toBeGreaterThan(before);
    expect(latest.upgrade.status).toBe("running");
  });
});

describe("target version", () => {
  it("adopts the backend's recommendation over the version we guessed", async () => {
    getUpgradeInfo.mockResolvedValue({
      provider: {},
      versionInfo: { recommendedTarget: "v1.30.4", latestAvailable: "v1.32.0" },
    });
    renderHook();
    await waitFor(() => expect(latest.targetVersion).toBe("v1.30.4"));
  });

  it("falls back to latest available when nothing is recommended", async () => {
    getUpgradeInfo.mockResolvedValue({
      provider: {},
      versionInfo: { latestAvailable: "v1.32.0" },
    });
    renderHook();
    await waitFor(() => expect(latest.targetVersion).toBe("v1.32.0"));
  });

  it("ignores an unknown latest version", async () => {
    getUpgradeInfo.mockResolvedValue({
      provider: {},
      versionInfo: { latestAvailable: "unknown" },
    });
    renderHook();
    await waitFor(() => expect(latest.targetVersion).toBe("v1.31.0"));
  });
});

describe("pure helpers", () => {
  it("classifies execution modes", () => {
    expect(isInstructionsOnly({ provider: { executionMode: "instructions" } })).toBe(true);
    expect(isInstructionsOnly({ provider: { upgradeSupported: false } })).toBe(true);
    expect(isInstructionsOnly({ provider: { executionMode: "plan-only" } })).toBe(false);
    expect(willExecuteAutomatically({ provider: executeProvider })).toBe(true);
    expect(willExecuteAutomatically({ provider: { executionMode: "plan-only" } })).toBe(false);
  });

  it("keeps steps from whichever field the response used", () => {
    expect(normalizeUpgradePayload({ steps: [1] }).upgradeSteps).toEqual([1]);
    expect(normalizeUpgradePayload({ upgradePlan: { steps: [2] } }).upgradeSteps).toEqual([2]);
    expect(normalizeUpgradePayload({}).upgradeSteps).toEqual([]);
  });
});
