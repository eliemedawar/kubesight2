import { describe, expect, it } from "vitest";
import { reconcileDialogStack, trapTargetIndex } from "./dialogFocus.js";

describe("trapTargetIndex", () => {
  it("steps forward and wraps from the last to the first", () => {
    expect(trapTargetIndex({ count: 3, index: 0 })).toBe(1);
    expect(trapTargetIndex({ count: 3, index: 2 })).toBe(0);
  });

  it("steps back and wraps from the first to the last", () => {
    expect(trapTargetIndex({ count: 3, index: 1, shift: true })).toBe(0);
    expect(trapTargetIndex({ count: 3, index: 0, shift: true })).toBe(2);
  });

  it("pulls focus that is outside the dialog back in", () => {
    expect(trapTargetIndex({ count: 4, inside: false })).toBe(0);
    expect(trapTargetIndex({ count: 4, inside: false, shift: true })).toBe(3);
  });

  it("moves from a non-tabbable spot to its neighbour in document order", () => {
    // Focus on the dialog container itself: everything follows it.
    expect(trapTargetIndex({ count: 3, insertAt: 0 })).toBe(0);
    expect(trapTargetIndex({ count: 3, insertAt: 0, shift: true })).toBe(2);
    // Focus on a scroll area between the 2nd and 3rd control.
    expect(trapTargetIndex({ count: 3, insertAt: 2 })).toBe(2);
    expect(trapTargetIndex({ count: 3, insertAt: 2, shift: true })).toBe(1);
    // After the last control.
    expect(trapTargetIndex({ count: 3, insertAt: 3 })).toBe(0);
  });

  it("has nowhere to go in an empty dialog", () => {
    expect(trapTargetIndex({ count: 0 })).toBe(-1);
  });
});

describe("reconcileDialogStack", () => {
  const a = { name: "a" };
  const b = { name: "b" };
  const c = { name: "c" };

  it("puts newly shown dialogs on top in document order", () => {
    const { stack, opened, closed } = reconcileDialogStack([], [a, b]);
    expect(stack.map((entry) => entry.element)).toEqual([a, b]);
    expect(opened.map((entry) => entry.element)).toEqual([a, b]);
    expect(closed).toEqual([]);
  });

  it("keeps the opening order, not the document order, for known dialogs", () => {
    // `b` opened first; `a` (earlier in the document) opened over it.
    const first = reconcileDialogStack([], [b]);
    const second = reconcileDialogStack(first.stack, [a, b]);
    expect(second.stack.map((entry) => entry.element)).toEqual([b, a]);
  });

  it("drops closed dialogs and keeps what they remembered", () => {
    const start = [{ element: a, returnTo: "row" }, { element: b, returnTo: "edit" }, { element: c }];
    const { stack, opened, closed } = reconcileDialogStack(start, [a]);
    expect(stack).toEqual([{ element: a, returnTo: "row" }]);
    expect(opened).toEqual([]);
    expect(closed.map((entry) => entry.returnTo)).toEqual(["edit", undefined]);
  });
});
