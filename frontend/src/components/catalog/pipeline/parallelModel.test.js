import { describe, expect, it } from "vitest";

import {
  buildSteps,
  canJoinPrevious,
  groupProblems,
  groupRuns,
  joinedToPrevious,
  joinPrevious,
  leaveGroup,
  MAX_GROUP_SIZE,
  normalizeGroups,
  parallelGroups,
  regroupAround,
  renameGroup,
  setGroupFailFast,
  stepStatus,
} from "./parallelModel.js";
import { blankStage, pipelineDiff, stageProblems, withKey } from "./stageModel.js";

const stage = (name, stageType = "command", extra = {}) =>
  withKey({ ...blankStage(stageType), name, commands: stageType === "command" ? [`run ${name}`] : [], ...extra });

const pipeline = () => [
  stage("Checkout", "checkout"),
  stage("Lint"),
  stage("Test"),
  stage("Sonar", "scan", { scan: { tool: "semgrep" } }),
  stage("Package"),
];

const groups = (stages) => stages.map((item) => item.parallelGroup || null);

describe("parallel groups — joining and leaving", () => {
  it("joins the stage before, starting a group when there is none", () => {
    const next = joinPrevious(pipeline(), 2);
    expect(groups(next)).toEqual([null, "Parallel 1", "Parallel 1", null, null]);
    expect(joinedToPrevious(next, 2)).toBe(true);
    expect(joinedToPrevious(next, 1)).toBe(false);
  });

  it("joins an existing group and takes its fail-fast switch", () => {
    let next = joinPrevious(pipeline(), 2);
    next = setGroupFailFast(next, 1, true);
    next = joinPrevious(next, 3);
    expect(groups(next)).toEqual([null, "Parallel 1", "Parallel 1", "Parallel 1", null]);
    expect(next.slice(1, 4).every((item) => item.parallelFailFast)).toBe(true);
  });

  it("refuses the checkout, server stages and the first stage", () => {
    expect(canJoinPrevious(pipeline(), 0).ok).toBe(false);
    expect(canJoinPrevious(pipeline(), 1).reason).toMatch(/checkout/i);
    const withDeploy = [...pipeline(), stage("Ship", "deploy")];
    expect(canJoinPrevious(withDeploy, 5).reason).toMatch(/KubeSight server/);
    // A refused join changes nothing.
    const base = pipeline();
    expect(joinPrevious(base, 1)).toBe(base);
  });

  it("stops at eight stages a group", () => {
    let stages = [stage("S0"), ...Array.from({ length: 9 }, (_, index) => stage(`S${index + 1}`))];
    for (let index = 1; index < MAX_GROUP_SIZE; index += 1) stages = joinPrevious(stages, index);
    expect(parallelGroups(stages)[0].indices).toHaveLength(MAX_GROUP_SIZE);
    expect(canJoinPrevious(stages, MAX_GROUP_SIZE).ok).toBe(false);
    expect(canJoinPrevious(stages, MAX_GROUP_SIZE).reason).toMatch(/8 stages/);
  });

  it("leaving from the middle splits the group in two", () => {
    let stages = [stage("A"), stage("B"), stage("C"), stage("D"), stage("E")];
    for (let index = 1; index < 5; index += 1) stages = joinPrevious(stages, index);
    const next = leaveGroup(stages, 2);
    const runs = parallelGroups(next);
    expect(runs.map((run) => run.indices)).toEqual([[0, 1], [3, 4]]);
    expect(runs[0].key).not.toBe(runs[1].key);
    expect(next[2].parallelGroup).toBeNull();
  });

  it("a group left with one stage is just that stage again", () => {
    const stages = joinPrevious(pipeline(), 2);
    const next = leaveGroup(stages, 2);
    expect(groups(next)).toEqual([null, null, null, null, null]);
  });

  it("renames every member at once", () => {
    const next = renameGroup(joinPrevious(pipeline(), 2), 1, "Checks");
    expect(groups(next)).toEqual([null, "Checks", "Checks", null, null]);
  });
});

describe("parallel groups — structural edits keep groups well-formed", () => {
  it("a stage dropped between two members joins their group", () => {
    const stages = joinPrevious(joinPrevious(pipeline(), 2), 3); // Lint, Test, Sonar
    const inserted = [...stages.slice(0, 2), stage("Audit"), ...stages.slice(2)];
    const next = regroupAround(inserted, 2);
    expect(parallelGroups(next)[0].indices).toEqual([1, 2, 3, 4]);
  });

  it("a stage that may not run in parallel dropped inside a group splits it, renamed", () => {
    const stages = joinPrevious(joinPrevious(joinPrevious(pipeline(), 2), 3), 4);
    const inserted = [...stages.slice(0, 3), stage("Ship", "deploy"), ...stages.slice(3)];
    const next = regroupAround(inserted, 3);
    const runs = parallelGroups(next);
    expect(runs.map((run) => run.indices)).toEqual([[1, 2], [4, 5]]);
    expect(runs[0].key).not.toBe(runs[1].key);
    expect(next[3].parallelGroup).toBeNull();
  });

  it("a member moved away from its group runs on its own", () => {
    const stages = joinPrevious(joinPrevious(pipeline(), 2), 3);
    const moved = [stages[0], stages[2], stages[3], stages[4], stages[1]]; // Lint to the end
    const next = regroupAround(moved, 4);
    expect(next[4].parallelGroup).toBeNull();
    expect(parallelGroups(next)[0].indices).toEqual([1, 2]);
  });

  it("normalizeGroups leaves a well-formed pipeline untouched", () => {
    const stages = joinPrevious(pipeline(), 2);
    expect(normalizeGroups(stages)).toBe(stages);
  });
});

describe("parallel groups — what a save would refuse", () => {
  it("mirrors the backend rules", () => {
    const split = [
      stage("Lint", "command", { parallelGroup: "g" }),
      stage("Build"),
      stage("Test", "command", { parallelGroup: "g" }),
    ];
    expect(groupProblems(split[2], 2, split)[0].message).toMatch(/split/);
    expect(groupProblems(split[0], 0, split)[0].message).toMatch(/at least two/);

    const checkout = [stage("Checkout", "checkout", { parallelGroup: "g" }), stage("Lint", "command", { parallelGroup: "g" })];
    expect(groupProblems(checkout[0], 0, checkout)[0].message).toMatch(/checkout/i);

    const off = [stage("A", "command", { parallelGroup: "g" }), stage("B", "command", { parallelGroup: "g", enabled: false })];
    expect(groupProblems(off[0], 0, off)[0].message).toMatch(/turned on/);

    const big = Array.from({ length: 9 }, (_, index) => stage(`S${index}`, "command", { parallelGroup: "g" }));
    expect(groupProblems(big[0], 0, big).some((item) => /at most 8/.test(item.message))).toBe(true);

    const fine = joinPrevious(pipeline(), 2);
    expect(groupProblems(fine[1], 1, fine)).toEqual([]);
    expect(stageProblems(fine[1], 1, fine, []).filter((item) => item.field === "parallel")).toEqual([]);
  });

  it("a stage saved without a group is not 'edited' by the editor's own defaults", () => {
    const saved = { stages: [{ ...stage("Lint"), parallelGroup: null, parallelFailFast: false }] };
    const draft = [{ ...saved.stages[0], parallelGroup: "", parallelFailFast: false }];
    expect(pipelineDiff(draft, [], saved).edited).toBe(0);
  });
});

describe("parallel groups on a build", () => {
  const built = [
    { id: 1, name: "Checkout", status: "success", parallelGroup: null },
    { id: 2, name: "Lint", status: "success", parallelGroup: "Checks" },
    { id: 3, name: "Test", status: "running", parallelGroup: "Checks", parallelFailFast: true },
    { id: 4, name: "Package", status: "pending", parallelGroup: null },
  ];

  it("folds consecutive members into one step", () => {
    const steps = buildSteps(built);
    expect(steps.map((step) => step.items.map((item) => item.name))).toEqual([
      ["Checkout"],
      ["Lint", "Test"],
      ["Package"],
    ]);
    expect(steps[1].group).toBe("Checks");
    expect(steps[1].failFast).toBe(true);
  });

  it("a lone member is an ordinary step", () => {
    const steps = buildSteps([{ id: 1, name: "Lint", parallelGroup: "Checks" }]);
    expect(steps[0].group).toBeNull();
  });

  it("summarises a group's status, worst first", () => {
    expect(stepStatus(built.slice(1, 3))).toBe("running");
    expect(stepStatus([{ status: "success" }, { status: "failed" }])).toBe("failed");
    expect(stepStatus([{ status: "success" }, { status: "skipped" }])).toBe("success");
    expect(stepStatus([{ status: "skipped" }, { status: "skipped" }])).toBe("skipped");
  });

  it("finds runs of one too, so the editor can explain them", () => {
    expect(groupRuns([stage("Lint", "command", { parallelGroup: "x" })])[0].indices).toEqual([0]);
  });
});
