import { describe, expect, it } from "vitest";

import {
  defaultScan,
  SCAN_TOOLS,
  scanArtifact,
  scanBlocks,
  scanProblems,
  scanSummary,
  scanToolPatch,
} from "./scanModel.js";
import {
  blankStage,
  changeKindPatch,
  defaultStageName,
  fieldsFor,
  fieldsLostOnKindChange,
  kindOf,
  STAGE_KINDS,
  stageFlags,
  stageProblems,
  stageSummary,
} from "./stageModel.js";

const scanStage = (tool, overrides = {}) => ({
  ...blankStage("scan"),
  name: "Security scan",
  scan: tool ? defaultScan(tool) : null,
  codeScan: tool === "semgrep" ? { enabled: true, tool: "semgrep", maxBlocking: 0, countFrom: "info", recipients: [] } : null,
  ...overrides,
});

describe("the scan kind", () => {
  it("is a fifth kind, offered with its own verb and icon", () => {
    expect(STAGE_KINDS.map((kind) => kind.value)).toEqual([
      "checkout",
      "command",
      "container_image",
      "scan",
      "deploy",
      "approval",
      "store_upload",
    ]);
    expect(kindOf("scan")).toMatchObject({ verb: "Scan code & dependencies", icon: "scan" });
    expect(defaultStageName("scan")).toBe("Security scan");
  });

  it("consumes no image, commands or files to keep — KubeSight writes those", () => {
    const fields = fieldsFor("scan");
    for (const field of ["image", "commands", "artifacts"]) expect(fields.has(field)).toBe(false);
    for (const field of ["scan", "codeScan", "env", "secrets", "runner"]) expect(fields.has(field)).toBe(true);
  });

  it("arrives with no scanner, and a save says to choose one", () => {
    const stage = { ...blankStage("scan"), name: "Scan" };
    expect(stage.scan).toBeNull();
    expect(stageProblems(stage, 0, [stage], [])).toContainEqual(expect.objectContaining({ field: "scan" }));
    expect(stageSummary(stage)).toBe("No scanner chosen yet");
  });

  it("clears its scanner, and names it, when it becomes another kind", () => {
    const stage = scanStage("trivy_fs");
    expect(changeKindPatch("command")).toMatchObject({ scan: null });
    expect(fieldsLostOnKindChange(stage, "command")).toContain("scanner settings");
    // ...and a command stage's script and image are what turning it into a scan loses.
    const command = { ...blankStage("command"), name: "Build", image: "node:22", commands: ["npm test"] };
    expect(fieldsLostOnKindChange(command, "scan")).toEqual(expect.arrayContaining(["container image", "commands"]));
    expect(changeKindPatch("scan")).toMatchObject({ commands: [], image: "" });
  });
});

describe("picking a tool", () => {
  it("offers the four tools, each with one sentence on what it finds", () => {
    expect(SCAN_TOOLS.map((tool) => tool.value)).toEqual(["trivy_fs", "semgrep", "dependency_check", "syft"]);
    for (const tool of SCAN_TOOLS) expect(tool.sentence.length).toBeGreaterThan(20);
  });

  it("gives Semgrep the quality gate and takes it away from every other tool", () => {
    const blank = { ...blankStage("scan"), name: "Scan" };
    const semgrep = scanToolPatch(blank, "semgrep");
    expect(semgrep.scan).toEqual({ tool: "semgrep", rules: [] });
    expect(semgrep.codeScan).toMatchObject({ enabled: true, tool: "semgrep", maxBlocking: 0 });

    // A gate the stage already had is kept, and switched on.
    const kept = scanToolPatch({ ...blank, codeScan: { enabled: false, maxBlocking: 4 } }, "semgrep");
    expect(kept.codeScan).toMatchObject({ enabled: true, maxBlocking: 4 });

    expect(scanToolPatch(scanStage("semgrep"), "trivy_fs")).toEqual({
      scan: defaultScan("trivy_fs"),
      codeScan: null,
    });
    // Choosing the tool it already has changes nothing — options survive.
    expect(scanToolPatch(scanStage("syft"), "syft")).toEqual({});
  });

  it("mirrors the backend's defaults", () => {
    expect(defaultScan("trivy_fs")).toEqual({
      tool: "trivy_fs",
      scanners: ["vuln", "secret"],
      threshold: "critical",
      onFail: "block",
      ignoreUnfixed: false,
      skipDirs: ["**/node_modules", "**/.git"],
    });
    expect(defaultScan("dependency_check")).toMatchObject({ failOnCvss: 7, onFail: "block" });
    expect(defaultScan("syft")).toEqual({ tool: "syft", format: "cyclonedx-json", target: "source" });
    expect(defaultScan("sonar")).toBeNull();
  });
});

describe("what a scan stage says and leaves", () => {
  it("reads in one line per tool", () => {
    expect(stageSummary(scanStage("trivy_fs"))).toBe("Trivy: dependencies, secrets · fails at critical");
    expect(stageSummary(scanStage("trivy_fs", { scan: { ...defaultScan("trivy_fs"), onFail: "warn", threshold: "high" } }))).toBe(
      "Trivy: dependencies, secrets · warns at high"
    );
    expect(stageSummary(scanStage("semgrep"))).toBe("Semgrep: rules for the app type · fails over 0 findings");
    expect(stageSummary(scanStage("dependency_check"))).toBe("Dependency-Check · fails at CVSS 7.0+");
    expect(stageSummary(scanStage("syft", { scan: { ...defaultScan("syft"), format: "spdx-json" } }))).toBe(
      "SBOM of the source · SPDX"
    );
  });

  it("names the artifact the backend collects", () => {
    expect(scanArtifact(defaultScan("trivy_fs"), 1)).toEqual({ name: "trivy-fs-stage-2.json", type: "scan-report" });
    expect(scanArtifact(defaultScan("semgrep"), 2)).toEqual({ name: "code-scan-stage-3.json", type: "scan-report" });
    expect(scanArtifact(defaultScan("dependency_check"), 0)).toEqual({
      name: "dependency-check-stage-1.json",
      type: "scan-report",
    });
    expect(scanArtifact({ ...defaultScan("syft"), format: "spdx-json" }, 3)).toEqual({
      name: "sbom-stage-4.spdx.json",
      type: "sbom",
    });
    expect(scanArtifact(null, 0)).toBeNull();
  });

  it("marks the stages whose findings fail the build", () => {
    expect(scanBlocks(scanStage("trivy_fs"))).toBe(true);
    expect(scanBlocks(scanStage("trivy_fs", { scan: { ...defaultScan("trivy_fs"), onFail: "warn" } }))).toBe(false);
    expect(scanBlocks(scanStage("semgrep"))).toBe(true);
    expect(scanBlocks(scanStage("syft"))).toBe(false);
    expect(stageFlags(scanStage("dependency_check")).map((flag) => flag.key)).toContain("gate");
  });
});

describe("what a save would refuse", () => {
  it("passes every tool's defaults", () => {
    for (const tool of SCAN_TOOLS) {
      const stage = scanStage(tool.value);
      expect(stageProblems(stage, 0, [stage], [])).toEqual([]);
    }
  });

  it("refuses the values the backend refuses", () => {
    const bad = (scan) => scanProblems({ scan }).map((problem) => problem.message);
    expect(bad({ ...defaultScan("trivy_fs"), skipDirs: ["$(rm -rf /)"] })[0]).toMatch(/skip pattern/);
    expect(bad({ tool: "semgrep", rules: ["p/java; curl evil"] })[0]).toMatch(/rule set/);
    expect(bad({ ...defaultScan("dependency_check"), failOnCvss: 11 })[0]).toMatch(/0 to 10/);
    expect(bad({ ...defaultScan("dependency_check"), failOnCvss: "" })[0]).toMatch(/0 to 10/);
    expect(bad({ ...defaultScan("dependency_check"), nvdDatafeedUrl: "ftp://mirror" })[0]).toMatch(/http\(s\)/);
    expect(bad({ ...defaultScan("dependency_check"), failOnCvss: 0 })).toEqual([]);
  });

  it("still checks a Semgrep scan stage's allowance like a command stage's", () => {
    const stage = scanStage("semgrep", { codeScan: { enabled: true, maxBlocking: -1 } });
    expect(stageProblems(stage, 0, [stage], [])).toContainEqual(expect.objectContaining({ field: "codeScan" }));
    // No "nothing runs semgrep" warning: KubeSight writes the semgrep line itself.
    const fine = scanStage("semgrep");
    expect(stageProblems(fine, 0, [fine], []).filter((problem) => problem.field === "codeScan")).toEqual([]);
  });
});
