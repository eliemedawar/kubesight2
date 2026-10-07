import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setVisibleInterval } from "./visibleInterval";

function fakeDocument() {
  const listeners = new Set();
  return {
    hidden: false,
    addEventListener: (type, fn) => type === "visibilitychange" && listeners.add(fn),
    removeEventListener: (type, fn) => type === "visibilitychange" && listeners.delete(fn),
    fire() {
      listeners.forEach((fn) => fn());
    },
    listenerCount: () => listeners.size,
  };
}

describe("setVisibleInterval", () => {
  let doc;

  beforeEach(() => {
    vi.useFakeTimers();
    doc = fakeDocument();
    vi.stubGlobal("document", doc);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("polls on schedule while visible", () => {
    const fn = vi.fn();
    const stop = setVisibleInterval(fn, 1000);
    vi.advanceTimersByTime(3000);
    expect(fn).toHaveBeenCalledTimes(3);
    stop();
  });

  it("skips ticks while hidden and catches up once when shown", () => {
    const fn = vi.fn();
    const stop = setVisibleInterval(fn, 1000);
    doc.hidden = true;
    vi.advanceTimersByTime(5000);
    expect(fn).not.toHaveBeenCalled();
    doc.hidden = false;
    doc.fire();
    expect(fn).toHaveBeenCalledTimes(1);
    stop();
  });

  it("does not refetch on a quick hide/show before a tick was due", () => {
    const fn = vi.fn();
    const stop = setVisibleInterval(fn, 1000);
    vi.advanceTimersByTime(300);
    doc.hidden = true;
    doc.fire();
    doc.hidden = false;
    doc.fire();
    expect(fn).not.toHaveBeenCalled();
    stop();
  });

  it("cleans up the timer and the listener", () => {
    const fn = vi.fn();
    const stop = setVisibleInterval(fn, 1000);
    expect(doc.listenerCount()).toBe(1);
    stop();
    vi.advanceTimersByTime(5000);
    expect(fn).not.toHaveBeenCalled();
    expect(doc.listenerCount()).toBe(0);
  });
});
