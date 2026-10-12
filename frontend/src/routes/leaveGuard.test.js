import { afterEach, describe, expect, it, vi } from "vitest";
import {
  blockingGuards,
  clearGuards,
  discardGuards,
  exitGuards,
  guardCount,
  hasUnloadGuards,
  historyDelta,
  historyIndex,
  historyState,
  leaveMessage,
  leavesPage,
  leavesRoute,
  registerGuard,
} from "./leaveGuard.js";
import { parseRoute } from "./routeUrl.js";

const pipelineTab = { key: "serviceDetail", params: { serviceId: "4", tab: "pipeline" }, query: {} };
const sameTabWithDrawer = { ...pipelineTab, query: { build: "12" } };
const settingsTab = { ...pipelineTab, params: { serviceId: "4", tab: "settings" } };
const otherPage = { key: "dashboard", params: {}, query: {} };

afterEach(() => clearGuards());

describe("leavesRoute", () => {
  it("treats another page, tab or record as leaving", () => {
    expect(leavesRoute(otherPage, pipelineTab)).toBe(true);
    expect(leavesRoute(settingsTab, pipelineTab)).toBe(true);
    expect(leavesRoute({ ...pipelineTab, params: { serviceId: "5", tab: "pipeline" } }, pipelineTab)).toBe(true);
  });

  it("treats a query-only change as staying", () => {
    expect(leavesRoute(sameTabWithDrawer, pipelineTab)).toBe(false);
  });

  it("compares params as strings, ignoring empty ones", () => {
    const numeric = { ...pipelineTab, params: { serviceId: 4, tab: "pipeline", extra: "" } };
    expect(leavesRoute(numeric, pipelineTab)).toBe(false);
  });
});

describe("leavesPage", () => {
  it("keeps moves between a page's own tabs and sub-routes", () => {
    const wizard = parseRoute("#/cluster-builder/new");
    const floor = parseRoute("#/cluster-builder");
    const detail = parseRoute("#/cluster-builder/builds/7");
    expect(wizard && floor && detail).toBeTruthy();
    expect(leavesPage(floor, wizard)).toBe(false);
    expect(leavesPage(detail, wizard)).toBe(false);
    expect(leavesPage(otherPage, wizard)).toBe(true);
  });
});

describe("guard registry", () => {
  it("blocks only while registered", () => {
    const unregister = registerGuard({ message: "Pipeline not saved" });
    expect(blockingGuards(otherPage, pipelineTab)).toHaveLength(1);
    unregister();
    expect(blockingGuards(otherPage, pipelineTab)).toHaveLength(0);
    expect(guardCount()).toBe(0);
  });

  it("lets a guard decide which moves lose its draft", () => {
    registerGuard({ message: "Wizard", blocks: leavesPage });
    const wizard = parseRoute("#/cluster-builder/new");
    expect(blockingGuards(parseRoute("#/cluster-builder"), wizard)).toHaveLength(0);
    expect(blockingGuards(otherPage, wizard)).toHaveLength(1);
  });

  it("falls back to the default rule when the predicate has no opinion", () => {
    registerGuard({ blocks: () => undefined });
    expect(blockingGuards(settingsTab, pipelineTab)).toHaveLength(1);
    expect(blockingGuards(sameTabWithDrawer, pipelineTab)).toHaveLength(0);
  });

  it("never traps the user behind a predicate that throws", () => {
    registerGuard({
      blocks: () => {
        throw new Error("boom");
      },
    });
    expect(blockingGuards(otherPage, pipelineTab)).toHaveLength(0);
  });

  it("discarding removes the guards and tells each page to forget its draft", () => {
    const onDiscard = vi.fn();
    registerGuard({ message: "a", onDiscard });
    registerGuard({ message: "b", blocks: () => false });
    const blocking = blockingGuards(otherPage, pipelineTab);
    discardGuards(blocking);
    expect(onDiscard).toHaveBeenCalledTimes(1);
    // Only the guard that asked is gone; the other still holds.
    expect(guardCount()).toBe(1);
  });

  it("asks about tab close only for guards that do not survive a reload", () => {
    expect(hasUnloadGuards()).toBe(false);
    const unregister = registerGuard({ unload: false });
    expect(hasUnloadGuards()).toBe(false);
    registerGuard({});
    expect(hasUnloadGuards()).toBe(true);
    unregister();
  });
});

describe("leaveMessage", () => {
  it("uses a default when no guard has a message", () => {
    registerGuard({});
    expect(leaveMessage(blockingGuards(otherPage, pipelineTab))).toMatch(/not saved/);
  });

  it("lets the newest guard speak and counts the rest", () => {
    registerGuard({ message: "Settings draft" });
    registerGuard({ message: "MCP tools draft" });
    const message = leaveMessage(blockingGuards(otherPage, pipelineTab));
    expect(message.startsWith("MCP tools draft")).toBe(true);
    expect(message).toMatch(/1 other unsaved change will be lost/);
  });

  it("does not repeat the same message", () => {
    registerGuard({ message: "Same" });
    registerGuard({ message: "Same" });
    expect(leaveMessage(blockingGuards(otherPage, pipelineTab))).toBe("Same");
  });
});

describe("history positions", () => {
  it("round-trips the index through history state", () => {
    expect(historyIndex(historyState(3))).toBe(3);
    expect(historyIndex(null)).toBe(null);
    expect(historyIndex({ ksIdx: "3" })).toBe(null);
  });

  it("measures Back and Forward so the router can step back the same distance", () => {
    expect(historyDelta(5, 4)).toBe(-1); // Back: restore with go(+1)
    expect(historyDelta(5, 7)).toBe(2); // Forward twice: restore with go(-2)
  });

  it("treats an entry with no index as a new one just past the current entry", () => {
    // A sidebar <a href>, a typed address: undo with go(-1).
    expect(historyDelta(5, null)).toBe(1);
    expect(historyDelta(null, null)).toBe(1);
  });
});

describe("exitGuards", () => {
  afterEach(() => clearGuards());

  it("asks for the drafts a sign-out loses, newest first, and skips reload-safe ones", () => {
    registerGuard({ message: "pipeline" });
    registerGuard({ message: "wizard", unload: false });
    registerGuard({ message: "settings" });
    expect(exitGuards().map(({ guard }) => guard.message)).toEqual(["settings", "pipeline"]);
  });

  it("is empty with nothing dirty", () => {
    expect(exitGuards()).toEqual([]);
  });
});
