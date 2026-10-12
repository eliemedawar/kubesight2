import { afterEach, describe, expect, it, vi } from "vitest";
import {
  adjustApprovalsWaiting,
  approvalsCountClaimed,
  approvalsWaitingAge,
  claimApprovalsCount,
  getApprovalsWaiting,
  resetApprovalsBadge,
  setApprovalsWaiting,
  subscribeApprovalsWaiting,
} from "./approvalsBadge";

afterEach(() => resetApprovalsBadge());

describe("approvalsBadge", () => {
  it("starts unknown, then holds what was published and tells listeners", () => {
    expect(getApprovalsWaiting()).toBeNull();
    expect(approvalsWaitingAge()).toBe(Infinity);
    const seen = vi.fn();
    const stop = subscribeApprovalsWaiting(seen);
    setApprovalsWaiting(4);
    expect(getApprovalsWaiting()).toBe(4);
    expect(seen).toHaveBeenCalledTimes(1);
    stop();
    setApprovalsWaiting(2);
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("ignores values that are not numbers", () => {
    setApprovalsWaiting(3);
    setApprovalsWaiting(undefined);
    setApprovalsWaiting("x");
    expect(getApprovalsWaiting()).toBe(3);
  });

  it("drops at once after a vote, never below zero, and not before a first count", () => {
    adjustApprovalsWaiting(-1);
    expect(getApprovalsWaiting()).toBeNull();
    setApprovalsWaiting(1);
    adjustApprovalsWaiting(-1);
    expect(getApprovalsWaiting()).toBe(0);
    adjustApprovalsWaiting(-1);
    expect(getApprovalsWaiting()).toBe(0);
  });

  it("knows how old the count is", () => {
    const now = Date.now();
    setApprovalsWaiting(1);
    expect(approvalsWaitingAge(now + 5000)).toBeGreaterThanOrEqual(4000);
    expect(approvalsWaitingAge(now + 5000)).toBeLessThanOrEqual(6000);
  });

  it("is claimed while any inbox holds it, and releasing twice is harmless", () => {
    expect(approvalsCountClaimed()).toBe(false);
    const releaseA = claimApprovalsCount();
    const releaseB = claimApprovalsCount();
    releaseA();
    releaseA();
    expect(approvalsCountClaimed()).toBe(true);
    releaseB();
    expect(approvalsCountClaimed()).toBe(false);
  });
});
