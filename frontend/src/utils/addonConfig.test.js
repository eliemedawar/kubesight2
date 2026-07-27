import { describe, expect, it } from "vitest";

import {
  addonConfigError,
  addonSelectionError,
  ipRangeListError,
  splitEntries,
} from "./addonConfig";

const METALLB = {
  id: "metallb",
  displayName: "MetalLB",
  configFields: [
    {
      key: "addressPools",
      type: "ipRangeList",
      label: "LoadBalancer address pool",
      required: true,
    },
  ],
};

const METRICS = { id: "metrics-server", displayName: "Metrics Server", configFields: [] };

describe("splitEntries", () => {
  it("accepts newline and comma separated input", () => {
    expect(splitEntries(" 10.0.0.1/32 \n10.0.0.5-10.0.0.9, 10.0.1.0/24 ")).toEqual([
      "10.0.0.1/32",
      "10.0.0.5-10.0.0.9",
      "10.0.1.0/24",
    ]);
  });

  it("is empty for blank input", () => {
    expect(splitEntries("")).toEqual([]);
    expect(splitEntries(null)).toEqual([]);
    expect(splitEntries("\n , \n")).toEqual([]);
  });
});

describe("ipRangeListError", () => {
  it("accepts CIDRs, bare addresses, and ranges", () => {
    expect(ipRangeListError("10.0.0.240/28")).toBe("");
    expect(ipRangeListError("10.0.0.240")).toBe("");
    expect(ipRangeListError("10.0.0.240-10.0.0.250\n10.0.1.0/24")).toBe("");
  });

  it("rejects empty, malformed, and out-of-range input", () => {
    expect(ipRangeListError("")).toMatch(/at least one/i);
    expect(ipRangeListError("not-an-address")).toMatch(/not a valid/i);
    expect(ipRangeListError("10.0.0.300")).toMatch(/not a valid/i);
    expect(ipRangeListError("10.0.0.1-")).toMatch(/start-end/i);
    expect(ipRangeListError("10.0.0.0/64")).toMatch(/prefix length/i);
    expect(ipRangeListError("10.0.0.0/24/8")).toMatch(/not a valid/i);
  });
});

describe("addonConfigError", () => {
  it("requires a pool for MetalLB", () => {
    expect(addonConfigError({ id: "metallb" }, METALLB)).toMatch(/required/i);
    expect(addonConfigError({ id: "metallb", config: { addressPools: "" } }, METALLB))
      .toMatch(/required/i);
  });

  it("accepts a pool as text or as the array the API returns", () => {
    expect(addonConfigError(
      { id: "metallb", config: { addressPools: "10.0.0.240-10.0.0.250" } },
      METALLB,
    )).toBe("");
    expect(addonConfigError(
      { id: "metallb", config: { addressPools: ["10.0.0.240/28"] } },
      METALLB,
    )).toBe("");
  });

  it("has nothing to say about add-ons that take no configuration", () => {
    expect(addonConfigError({ id: "metrics-server" }, METRICS)).toBe("");
    expect(addonConfigError({ id: "unknown" }, undefined)).toBe("");
  });
});

describe("addonSelectionError", () => {
  const catalog = [METALLB, METRICS];

  it("names the add-on that is not ready", () => {
    expect(addonSelectionError([{ id: "metrics-server" }, { id: "metallb" }], catalog))
      .toMatch(/^MetalLB: /);
  });

  it("is empty once every selection is valid", () => {
    expect(addonSelectionError([
      { id: "metrics-server" },
      { id: "metallb", config: { addressPools: "10.0.0.240/28" } },
    ], catalog)).toBe("");
    expect(addonSelectionError([], catalog)).toBe("");
  });
});
