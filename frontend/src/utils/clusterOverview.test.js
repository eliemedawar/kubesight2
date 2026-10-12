import { describe, expect, it } from "vitest";

import { clusterFacts, countValue, cpuValue, memoryValue, statusTone, statusValue, usagePercent } from "./clusterOverview.js";

describe("cluster overview formatting", () => {
  it("shows unknown as a dash, never 0", () => {
    expect(countValue(null)).toBe("—");
    expect(countValue(undefined)).toBe("—");
    expect(countValue(0)).toBe("0");
    expect(countValue(1298)).toBe((1298).toLocaleString());
    expect(usagePercent(null, 256)).toBeNull();
    expect(usagePercent(10, 0)).toBeNull();
    expect(cpuValue({ usedCores: null, capacityCores: 256 })).toBe("— / 256 cores");
    expect(memoryValue(null)).toBe("—");
  });

  it("formats without forced decimals", () => {
    expect(cpuValue({ usedCores: 191, capacityCores: 256 })).toBe("191 / 256 cores");
    expect(cpuValue({ usedCores: 0.25, capacityCores: 4 })).toBe("0.25 / 4 cores");
    expect(usagePercent(191, 256)).toBe(75);
    expect(statusValue("healthy")).toBe("Healthy");
    expect(statusValue("")).toBe("Unknown");
  });

  it("maps statuses to tones and builds the facts line from real fields only", () => {
    expect(statusTone("Warning")).toBe("warn");
    expect(statusTone("weird")).toBe("info");
    expect(clusterFacts({ provider: "aws", region: "us-east-1", k8sVersion: "v1.30.2", nodes: 18 })).toBe(
      "AWS · us-east-1 · Kubernetes v1.30.2 · 18 nodes",
    );
    expect(clusterFacts({ provider: "", region: "unknown", version: "unknown", nodes: null })).toBe("");
    expect(clusterFacts({ nodes: 1 })).toBe("1 node");
  });
});
