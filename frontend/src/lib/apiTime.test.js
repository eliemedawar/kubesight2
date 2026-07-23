import { describe, expect, it } from "vitest";
import { parseApiTime } from "./apiTime";

describe("parseApiTime", () => {
  it("treats API timestamps without a timezone as UTC", () => {
    expect(parseApiTime("2026-07-23T11:48:00")).toBe(
      Date.parse("2026-07-23T11:48:00Z"),
    );
  });

  it("preserves explicit timezone offsets", () => {
    expect(parseApiTime("2026-07-23T14:48:00+03:00")).toBe(
      Date.parse("2026-07-23T14:48:00+03:00"),
    );
  });

  it("returns NaN for missing or invalid timestamps", () => {
    expect(parseApiTime(null)).toBeNaN();
    expect(parseApiTime("not-a-date")).toBeNaN();
  });
});
