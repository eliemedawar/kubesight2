import { describe, expect, it } from "vitest";
import { describeLoadError, shouldShowAccessError } from "./authz.js";

/**
 * The trap this helper exists to close.
 *
 * `shouldShowAccessError` reads like "should I show this error" and does not
 * mean that — it is true only for access-denied messages, because its job was
 * to spot an unexpected 403. Anything using it as a general gate displayed 403s
 * and silently swallowed everything else.
 */
describe("the misreading that caused the bug", () => {
  it("shouldShowAccessError is false for ordinary errors", () => {
    expect(shouldShowAccessError("Something broke")).toBe(false);
    expect(shouldShowAccessError("Request failed (500)")).toBe(false);
  });

  it("...and true for access denials, which is the opposite of how it reads", () => {
    expect(shouldShowAccessError("Forbidden")).toBe(true);
  });
});

describe("describeLoadError", () => {
  // The regression that mattered: a 500 on the dashboard, inventory or upgrade
  // rendered an empty page with no explanation.
  it("shows an ordinary failure", () => {
    expect(describeLoadError("Request failed (500)")).toBe("Request failed (500)");
    expect(describeLoadError("Something broke")).toBe("Something broke");
  });

  it("translates an access denial into the standard wording", () => {
    expect(describeLoadError("Forbidden")).toBe("You do not have access to this resource.");
    expect(describeLoadError("permission denied")).toBe(
      "You do not have access to this resource."
    );
  });

  // Telling someone they cannot see a cluster we already know they cannot see
  // is noise, and it fires on every poll.
  it("stays quiet about a denial we expected", () => {
    expect(describeLoadError("Forbidden", { expectedDenied: true })).toBe("");
    expect(describeLoadError("Request failed (500)", { expectedDenied: true })).toBe("");
  });

  it("has nothing to say about a missing message", () => {
    expect(describeLoadError("")).toBe("");
    expect(describeLoadError(null)).toBe("");
    expect(describeLoadError(undefined)).toBe("");
  });
});
