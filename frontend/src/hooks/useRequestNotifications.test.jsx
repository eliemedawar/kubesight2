// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listMyDeploymentRequests = vi.fn();
vi.mock("../api/deploymentRequestsApi.js", () => ({
  listMyDeploymentRequests: (...a) => listMyDeploymentRequests(...a),
}));

const { useRequestNotifications } = await import("./useRequestNotifications.js");

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.localStorage.clear();
});
beforeEach(() => vi.clearAllMocks());

let latest = null;
function Probe(props) {
  latest = useRequestNotifications(props);
  return null;
}

const request = (id, over = {}) => ({
  id,
  status: "approved",
  decidedAt: "2026-08-02T10:00:00Z",
  createdAt: "2026-08-02T09:00:00Z",
  ...over,
});

const renderHook = (props = {}) =>
  render(<Probe enabled userId="u1" {...props} />);

describe("what reaches the bell", () => {
  it("shows decided requests, newest first", async () => {
    listMyDeploymentRequests.mockResolvedValue({
      items: [
        request("old", { decidedAt: "2026-08-02T09:00:00Z" }),
        request("new", { decidedAt: "2026-08-02T11:00:00Z" }),
      ],
    });
    renderHook();
    await waitFor(() => expect(latest.items).toHaveLength(2));
    expect(latest.items.map((r) => r.id)).toEqual(["new", "old"]);
  });

  // The bell is for decisions. A request still waiting is not news.
  it("ignores requests that are still pending", async () => {
    listMyDeploymentRequests.mockResolvedValue({
      items: [request("a"), request("b", { status: "pending" })],
    });
    renderHook();
    await waitFor(() => expect(latest.items).toHaveLength(1));
    expect(latest.items[0].id).toBe("a");
  });

  it("fetches nothing without the permission", async () => {
    renderHook({ enabled: false });
    await waitFor(() => expect(latest.items).toEqual([]));
    expect(listMyDeploymentRequests).not.toHaveBeenCalled();
  });
});

describe("seen and dismissed are different things", () => {
  // Collapsing them would mean opening the bell threw the list away.
  it("marking seen clears the count but keeps the items", async () => {
    listMyDeploymentRequests.mockResolvedValue({ items: [request("a"), request("b")] });
    renderHook();
    await waitFor(() => expect(latest.unseenCount).toBe(2));

    act(() => latest.markAllSeen());

    expect(latest.unseenCount).toBe(0);
    expect(latest.items).toHaveLength(2);
  });

  it("dismissing removes the item", async () => {
    listMyDeploymentRequests.mockResolvedValue({ items: [request("a"), request("b")] });
    renderHook();
    await waitFor(() => expect(latest.items).toHaveLength(2));

    act(() => latest.dismiss(request("a")));

    expect(latest.items.map((r) => r.id)).toEqual(["b"]);
  });

  it("dismissing all empties the bell", async () => {
    listMyDeploymentRequests.mockResolvedValue({ items: [request("a"), request("b")] });
    renderHook();
    await waitFor(() => expect(latest.items).toHaveLength(2));

    act(() => latest.dismissAll());

    expect(latest.items).toEqual([]);
    expect(latest.unseenCount).toBe(0);
  });
});

describe("read state is per user", () => {
  // A shared machine must not carry one person's read state into the next
  // person's bell.
  it("does not leak seen state between users", async () => {
    listMyDeploymentRequests.mockResolvedValue({ items: [request("a")] });
    const { rerender } = renderHook({ userId: "u1" });
    await waitFor(() => expect(latest.unseenCount).toBe(1));
    act(() => latest.markAllSeen());
    expect(latest.unseenCount).toBe(0);

    await act(async () => rerender(<Probe enabled userId="u2" />));
    await waitFor(() => expect(latest.unseenCount).toBe(1));
  });

  it("restores seen state for the same user", async () => {
    listMyDeploymentRequests.mockResolvedValue({ items: [request("a")] });
    const first = renderHook({ userId: "u1" });
    await waitFor(() => expect(latest.unseenCount).toBe(1));
    act(() => latest.markAllSeen());
    first.unmount();

    renderHook({ userId: "u1" });
    await waitFor(() => expect(latest.items).toHaveLength(1));
    expect(latest.unseenCount).toBe(0);
  });

  // Re-deciding a request is a new thing to be told about, so the signature is
  // the decision rather than the request.
  it("treats a re-decided request as unseen again", async () => {
    listMyDeploymentRequests.mockResolvedValue({
      items: [request("a", { decidedAt: "2026-08-02T10:00:00Z" })],
    });
    const first = renderHook();
    await waitFor(() => expect(latest.unseenCount).toBe(1));
    act(() => latest.markAllSeen());
    first.unmount();

    listMyDeploymentRequests.mockResolvedValue({
      items: [request("a", { decidedAt: "2026-08-03T10:00:00Z" })],
    });
    renderHook();
    await waitFor(() => expect(latest.unseenCount).toBe(1));
  });
});

describe("failure and polling", () => {
  it("empties rather than throwing when the request fails", async () => {
    listMyDeploymentRequests.mockRejectedValue(new Error("network"));
    renderHook();
    await waitFor(() => expect(latest.items).toEqual([]));
  });

  it("stops polling on unmount", async () => {
    vi.useFakeTimers();
    listMyDeploymentRequests.mockResolvedValue({ items: [] });
    const { unmount } = renderHook();
    await act(async () => {});
    const before = listMyDeploymentRequests.mock.calls.length;

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });

    expect(listMyDeploymentRequests.mock.calls.length).toBe(before);
  });
});
