// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getDashboardSummary = vi.fn();
vi.mock("../api/dashboardApi.js", () => ({
  getDashboardSummary: (...args) => getDashboardSummary(...args),
}));

const { useDashboardSummary } = await import("./useDashboardSummary.js");

/**
 * The guards in this hook are the reason it is worth testing: both are
 * invisible when things are fast, and both produce wrong numbers under a name
 * that says they are right.
 */

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
beforeEach(() => vi.clearAllMocks());

let latest = null;

function Probe(props) {
  latest = useDashboardSummary(props);
  return null;
}

const allowAll = () => true;

const renderHook = (props = {}) => {
  const utils = render(
    <Probe clusterId="prod-eu" canAccessCluster={allowAll} {...props} />
  );
  return {
    ...utils,
    update: (next) =>
      act(() => utils.rerender(<Probe clusterId="prod-eu" canAccessCluster={allowAll} {...props} {...next} />)),
  };
};

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

describe("stale responses never overwrite newer ones", () => {
  // A slow poll landing after a fast one used to repaint older numbers over
  // newer ones, on a page people watch specifically for change.
  it("ignores an earlier request that resolves last", async () => {
    const first = deferred();
    const second = deferred();
    getDashboardSummary.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    renderHook();
    await act(async () => {
      latest.refresh();
    });

    await act(async () => {
      second.resolve({ clusterId: "prod-eu", marker: "newer" });
      await second.promise;
    });
    await act(async () => {
      first.resolve({ clusterId: "prod-eu", marker: "older" });
      await first.promise;
    });

    expect(latest.summary.marker).toBe("newer");
  });

  // Switching cluster mid-flight would otherwise paint the old cluster's
  // numbers under the new cluster's name.
  it("discards a response for a cluster that is no longer selected", async () => {
    const slow = deferred();
    getDashboardSummary.mockReturnValueOnce(slow.promise);
    const probe = renderHook({ clusterId: "prod-eu" });

    getDashboardSummary.mockResolvedValueOnce({ clusterId: "prod-us", marker: "us" });
    probe.update({ clusterId: "prod-us" });
    await waitFor(() => expect(latest.summary?.marker).toBe("us"));

    await act(async () => {
      slow.resolve({ clusterId: "prod-eu", marker: "eu" });
      await slow.promise;
    });

    expect(latest.summary.marker).toBe("us");
  });

  it("clears the old cluster's numbers when the cluster changes", async () => {
    getDashboardSummary.mockResolvedValueOnce({ clusterId: "prod-eu", marker: "eu" });
    const probe = renderHook({ clusterId: "prod-eu" });
    await waitFor(() => expect(latest.summary?.marker).toBe("eu"));

    getDashboardSummary.mockReturnValueOnce(deferred().promise);
    probe.update({ clusterId: "prod-us" });

    // Nothing from the previous cluster survives under the new name.
    expect(latest.summary).toBeNull();
  });
});

describe("a deleted cluster", () => {
  it("reports upward instead of polling a dead id", async () => {
    const error = new Error("Not found");
    error.status = 404;
    getDashboardSummary.mockRejectedValue(error);
    const onClusterMissing = vi.fn();

    renderHook({ onClusterMissing });

    await waitFor(() => expect(onClusterMissing).toHaveBeenCalled());
    expect(latest.summary).toBeNull();
    // A 404 is handled, not surfaced as an error the operator must read.
    expect(latest.error).toBe("");
  });
});

describe("permissions", () => {
  it("does not fetch a cluster the user cannot access", async () => {
    renderHook({ canAccessCluster: () => false });
    await waitFor(() => expect(latest.loading).toBe(false));
    expect(getDashboardSummary).not.toHaveBeenCalled();
    expect(latest.summary).toBeNull();
  });

  it("does not fetch when disabled or without a cluster", async () => {
    renderHook({ enabled: false });
    renderHook({ clusterId: "" });
    expect(getDashboardSummary).not.toHaveBeenCalled();
  });
});

describe("polling", () => {
  it("stops when the component unmounts", async () => {
    vi.useFakeTimers();
    getDashboardSummary.mockResolvedValue({ clusterId: "prod-eu" });
    const { unmount } = render(
      <Probe clusterId="prod-eu" canAccessCluster={allowAll} refreshIntervalSeconds={30} />
    );
    await act(async () => {});
    const callsBefore = getDashboardSummary.mock.calls.length;

    unmount();
    await act(async () => {
      vi.advanceTimersByTime(120_000);
    });

    // The route's lifetime is the poller's lifetime; there is no page check to
    // get wrong any more.
    expect(getDashboardSummary.mock.calls.length).toBe(callsBefore);
  });

  it("clamps the interval to between 30 and 60 seconds", async () => {
    vi.useFakeTimers();
    getDashboardSummary.mockResolvedValue({ clusterId: "prod-eu" });
    render(<Probe clusterId="prod-eu" canAccessCluster={allowAll} refreshIntervalSeconds={1} />);
    await act(async () => {});
    const initial = getDashboardSummary.mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(29_000);
    });
    expect(getDashboardSummary.mock.calls.length).toBe(initial);

    await act(async () => {
      vi.advanceTimersByTime(2_000);
    });
    expect(getDashboardSummary.mock.calls.length).toBeGreaterThan(initial);
  });
});

describe("foreground and background loads are distinguishable", () => {
  it("reports refreshing rather than loading on a background poll", async () => {
    vi.useFakeTimers();
    getDashboardSummary.mockResolvedValue({ clusterId: "prod-eu" });
    render(<Probe clusterId="prod-eu" canAccessCluster={allowAll} refreshIntervalSeconds={30} />);
    await act(async () => {});

    let pending = deferred();
    getDashboardSummary.mockReturnValueOnce(pending.promise);
    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    // A poll must not blank the page it is refreshing.
    expect(latest.loading).toBe(false);
    expect(latest.refreshing).toBe(true);

    await act(async () => {
      pending.resolve({ clusterId: "prod-eu" });
      await pending.promise;
    });
    expect(latest.refreshing).toBe(false);
  });
});
