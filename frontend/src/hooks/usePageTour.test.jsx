// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getTourSteps = vi.fn();
const getWelcomeSteps = vi.fn();
vi.mock("../tours/tourDefinitions.js", () => ({
  getTourSteps: (...a) => getTourSteps(...a),
  getWelcomeSteps: (...a) => getWelcomeSteps(...a),
  WELCOME_TOUR_KEY: "welcome",
}));

const { usePageTour } = await import("./usePageTour.js");

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.localStorage.clear();
});
beforeEach(() => {
  vi.clearAllMocks();
  getTourSteps.mockReturnValue([{ target: ".x", title: "t" }]);
  getWelcomeSteps.mockReturnValue([{ target: ".w", title: "welcome" }]);
});

let latest = null;
function Probe(props) {
  latest = usePageTour(props);
  return null;
}

const base = { userId: "u1", enabled: true, isAdmin: false, hasPermission: () => true, pageAllowed: () => true };
const renderHook = (props = {}) => render(<Probe {...base} pageKey="dashboard" {...props} />);

describe("auto-run", () => {
  it("starts after a delay so the page can render its targets", async () => {
    vi.useFakeTimers();
    renderHook();
    expect(latest.activeTour).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour?.pageKey).toBe("dashboard");
    expect(latest.activeTour?.auto).toBe(true);
  });

  it("runs once per page per user", async () => {
    vi.useFakeTimers();
    const first = renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    act(() => latest.close());
    first.unmount();

    renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeNull();
  });

  it("does not run when muted", async () => {
    vi.useFakeTimers();
    const first = renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    act(() => latest.mute());
    first.unmount();

    renderHook({ pageKey: "alerts" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeNull();
  });

  it("does not run before the user is ready", async () => {
    vi.useFakeTimers();
    renderHook({ enabled: false });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeNull();
  });

  it("does not run on a page with no steps", async () => {
    vi.useFakeTimers();
    getTourSteps.mockReturnValue([]);
    renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeNull();
  });

  it("does not run where there is no page", async () => {
    vi.useFakeTimers();
    renderHook({ pageKey: null });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeNull();
  });
});

describe("navigating away", () => {
  // Without marking it seen, a tour interrupted by a click would re-run on
  // every later visit — which is how a helpful thing becomes a nagging one.
  it("ends the tour and counts it as seen", async () => {
    vi.useFakeTimers();
    const { rerender } = renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeTruthy();

    await act(async () => rerender(<Probe {...base} pageKey="alerts" />));
    expect(latest.activeTour).toBeNull();

    // Returning does not restart it.
    await act(async () => rerender(<Probe {...base} pageKey="dashboard" />));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeNull();
  });

  // Signing out must not mark the outgoing user's tours seen for the next one.
  it("clears without marking seen when the session ends", async () => {
    vi.useFakeTimers();
    const { rerender } = renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });

    await act(async () => rerender(<Probe {...base} pageKey="dashboard" enabled={false} />));
    expect(latest.activeTour).toBeNull();

    await act(async () => rerender(<Probe {...base} pageKey="dashboard" enabled />));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour).toBeTruthy();
  });
});

describe("replay from the topbar", () => {
  it("starts immediately and is not marked auto", () => {
    renderHook();
    act(() => latest.start());
    expect(latest.activeTour?.auto).toBe(false);
  });

  it("replays even after the tour has been seen", async () => {
    vi.useFakeTimers();
    renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    act(() => latest.close());
    expect(latest.activeTour).toBeNull();

    act(() => latest.start());
    expect(latest.activeTour).toBeTruthy();
  });
});

describe("the welcome tour", () => {
  it("is prepended the first time and not after", async () => {
    vi.useFakeTimers();
    const first = renderHook();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour.includesWelcome).toBe(true);
    expect(latest.activeTour.steps).toHaveLength(2);
    act(() => latest.close());
    first.unmount();

    renderHook({ pageKey: "alerts" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(latest.activeTour.includesWelcome).toBe(false);
    expect(latest.activeTour.steps).toHaveLength(1);
  });
});
