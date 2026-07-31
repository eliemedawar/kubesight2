import { describe, expect, it } from "vitest";

import {
  coverageTone,
  isAnalysisActive,
  normalizeDropdownNames,
  riskLevelTone,
  sortFindings,
  validateApplicationForm,
} from "./applicationIntelligence";

describe("Application Intelligence form and status", () => {
  it("rejects unsafe or incomplete analysis input", () => {
    const errors = validateApplicationForm({
      name: "",
      repositoryUrl: "https://github.com/workspace/repository",
      credentialProfileId: "",
      repositorySubdirectory: "../service",
      dockerfilePath: "/Dockerfile",
    });
    expect(Object.keys(errors)).toEqual(
      expect.arrayContaining([
        "name",
        "repositoryUrl",
        "credentialProfileId",
        "repositorySubdirectory",
        "dockerfilePath",
      ])
    );
  });

  it("accepts a safe Bitbucket analysis form without retaining credentials", () => {
    const form = {
      name: "Payment Service",
      repositoryUrl: "https://bitbucket.org/workspace/payment-service",
      credentialProfileId: 12,
      repositorySubdirectory: "services/payment",
      dockerfilePath: "services/payment/Dockerfile",
    };
    expect(validateApplicationForm(form)).toEqual({});
    expect(form).not.toHaveProperty("token");
    expect(form).not.toHaveProperty("password");
  });

  it("classifies progress and completed states", () => {
    expect(isAnalysisActive("Scanning")).toBe(true);
    expect(isAnalysisActive("Completed With Warnings")).toBe(false);
    expect(isAnalysisActive("Failed")).toBe(false);
  });

  it("derives risk tone from severity level rather than an invented score", () => {
    expect(riskLevelTone("Critical")).toBe("fail");
    expect(riskLevelTone("Medium")).toBe("warning");
    expect(riskLevelTone("None")).toBe("pass");
  });

  it("treats an absent deterministic scanner as unmeasured, not clean", () => {
    expect(coverageTone("Full")).toBe("pass");
    expect(coverageTone("Partial")).toBe("warning");
    expect(coverageTone("Hermes only")).toBe("fail");
  });

  it("orders findings by severity, then by strength of evidence", () => {
    const ordered = sortFindings([
      { title: "b", severity: "Medium", confidence: "High" },
      { title: "a", severity: "Critical", confidence: "Low" },
      { title: "c", severity: "Medium", confidence: "Confirmed" },
    ]);
    expect(ordered.map((item) => item.title)).toEqual(["a", "c", "b"]);
  });

  it("normalizes API responses for dependent dropdowns", () => {
    expect(
      normalizeDropdownNames({
        items: ["payments", { name: "default" }, "payments", { name: "" }],
      })
    ).toEqual(["default", "payments"]);
    expect(normalizeDropdownNames({ namespaces: [{ name: "production" }] })).toEqual([
      "production",
    ]);
    expect(normalizeDropdownNames({ items: "invalid" })).toEqual([]);
  });
});
