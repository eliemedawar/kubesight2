import { describe, expect, it } from "vitest";

import {
  blankStage,
  changeKindPatch,
  codeScanArmed,
  fieldsLostOnKindChange,
  runsSemgrep,
  stageProblems,
} from "./stageModel.js";

const scanStage = (overrides = {}) => ({
  ...blankStage("command"),
  name: "Scan Source Code",
  commands: ["semgrep scan --config p/typescript ."],
  codeScan: { enabled: true, tool: "semgrep", maxBlocking: 5, countFrom: "info", recipients: [] },
  ...overrides,
});

describe("code scan quality gate", () => {
  it("is armed only when present and not switched off", () => {
    expect(codeScanArmed(blankStage("command"))).toBe(false);
    expect(codeScanArmed(scanStage())).toBe(true);
    expect(codeScanArmed(scanStage({ codeScan: { enabled: false } }))).toBe(false);
  });

  it("recognises a semgrep command however it is written", () => {
    expect(runsSemgrep(scanStage())).toBe(true);
    expect(runsSemgrep(scanStage({ commands: ["cd app && semgrep ci"] }))).toBe(true);
    expect(runsSemgrep(scanStage({ commands: ["npm run lint", "echo semgrep-free"] }))).toBe(false);
  });

  it("warns when the gate is on but nothing runs semgrep", () => {
    const problems = stageProblems(scanStage({ commands: ["npm test"] }), 0, [], []);
    expect(problems).toContainEqual(expect.objectContaining({ field: "codeScan", level: "warning" }));
    expect(stageProblems(scanStage(), 0, [], []).filter((item) => item.field === "codeScan")).toEqual([]);
  });

  it("refuses an allowance that is not a whole number", () => {
    const problems = stageProblems(scanStage({ codeScan: { enabled: true, maxBlocking: -2 } }), 0, [], []);
    expect(problems).toContainEqual(expect.objectContaining({ field: "codeScan" }));
    expect(problems.find((item) => item.field === "codeScan").level).toBeUndefined();
  });

  it("is cleared, and named, when the stage stops being a command stage", () => {
    expect(changeKindPatch("container_image")).toMatchObject({ codeScan: null });
    expect(fieldsLostOnKindChange(scanStage(), "container_image")).toContain("quality gate");
  });
});
