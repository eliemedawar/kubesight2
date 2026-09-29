import { describe, expect, it } from "vitest";

import {
  activeTools,
  dirtyKeys,
  gateNeverBlocks,
  readiness,
  rebaseForm,
  runBreakdown,
  scriptKeepsMetric,
  toForm,
  watchedBranches,
} from "./mergeCheckModel.js";

const config = {
  sourceReady: true,
  canReportVerdict: { ok: true },
  recommendedTools: ["eslint", "semgrep"],
  targetBranches: [],
};

const hook = { known: true, exists: true, inSync: true, active: true, secretSet: true, repository: "acme/app" };
const enforced = { known: true, enforced: true, hardBlock: true, covered: ["main"] };

describe("readiness: is a failing pull request really stopped?", () => {
  it("is protected only when all four steps are done", () => {
    const status = readiness({ config, webhook: hook, enforcement: enforced, enabled: true });
    expect(status.overall).toBe("protected");
    expect(status.done).toBe(4);
  });

  it("never counts an unanswered Bitbucket question as done", () => {
    const status = readiness({
      config,
      webhook: { known: false, reason: "No public address." },
      enforcement: { known: false, reason: "Needs repo admin." },
      enabled: true,
    });
    expect(status.steps.map((step) => step.state)).toEqual(["ok", "unknown", "unknown", "ok"]);
    expect(status.overall).toBe("partial");
  });

  it("calls a protected branch with checks off stuck, not merely unprotected", () => {
    // Bitbucket waits for a status that is never posted.
    const status = readiness({ config, webhook: hook, enforcement: enforced, enabled: false });
    expect(status.overall).toBe("stuck");
  });

  it("says why the repository cannot take a verdict and where to fix it", () => {
    const status = readiness({
      config: { ...config, canReportVerdict: { ok: false, reason: "Credential is read-only." } },
      webhook: hook,
      enforcement: enforced,
      enabled: true,
    });
    expect(status.steps[0]).toMatchObject({ state: "todo", action: "credential", detail: "Credential is read-only." });
  });

  it("treats a warning-only restriction as needing attention", () => {
    const status = readiness({
      config,
      webhook: hook,
      enforcement: { known: true, enforced: true, hardBlock: false },
      enabled: true,
    });
    expect(status.steps[2].state).toBe("warn");
  });

  it("names what is wrong with an out-of-sync webhook", () => {
    const status = readiness({
      config,
      webhook: { ...hook, inSync: false, secretSet: false, missingEvents: ["pullrequest:updated"] },
      enforcement: enforced,
      enabled: true,
    });
    expect(status.steps[1].detail).toMatch(/no secret and it does not send pullrequest:updated/);
    expect(status.steps[1].action).toBe("setup");
  });
});

describe("form state", () => {
  const saved = toForm({ enabled: false, tools: ["eslint"], events: ["pullrequest:created"], statusKey: "K" });

  it("ignores the switch and list order when finding unsaved changes", () => {
    expect(dirtyKeys({ ...saved, enabled: true }, saved)).toEqual([]);
    expect(dirtyKeys({ ...saved, events: ["pullrequest:created"] }, saved)).toEqual([]);
    expect(dirtyKeys({ ...saved, statusKey: "OTHER" }, saved)).toEqual(["statusKey"]);
  });

  it("keeps an edit in progress across a partial save", () => {
    const editing = { ...saved, statusKey: "MINE" };
    const fromServer = { ...saved, enabled: true, tools: ["eslint", "semgrep"] };
    const next = rebaseForm(editing, saved, fromServer);
    expect(next).toMatchObject({ statusKey: "MINE", enabled: true, tools: ["eslint", "semgrep"] });
  });
});

describe("what runs", () => {
  it("follows the recommendation in automatic mode and the choice otherwise", () => {
    expect(activeTools(config, { toolsMode: "auto", tools: ["pmd"] })).toEqual(["eslint", "semgrep"]);
    expect(activeTools(config, { toolsMode: "custom", tools: ["pmd"] })).toEqual(["pmd"]);
  });

  it("falls back to the default branch when no branch is listed", () => {
    expect(watchedBranches(config, { defaultBranch: "master" })).toEqual(["master"]);
    expect(watchedBranches({ targetBranches: ["release/*"] }, {})).toEqual(["release/*"]);
  });

  it("notices a gate that can never block", () => {
    expect(gateNeverBlocks({ maxTotalProblems: null, maxEslintProblems: null }, ["eslint"])).toBe(true);
    expect(gateNeverBlocks({ maxTotalProblems: null, maxEslintProblems: 0 }, ["eslint"])).toBe(false);
    // A cap on a check that does not run does not make the gate bite.
    expect(gateNeverBlocks({ maxTotalProblems: null, maxPmdProblems: 0 }, ["eslint"])).toBe(true);
  });
});

describe("history", () => {
  it("lists each check's findings in tool order with its cap", () => {
    const rows = runBreakdown({
      metrics: { semgrep: { status: "ok", problems: 2 }, eslint: { status: "error", problems: 0 } },
      gate: { maxEslintProblems: 0 },
    });
    expect(rows.map((row) => [row.tool, row.status, row.problems, row.cap])).toEqual([
      ["eslint", "error", 0, 0],
      ["semgrep", "ok", 2, undefined],
    ]);
  });
});

describe("edited scripts", () => {
  it("must keep the metric line for their own tool", () => {
    expect(scriptKeepsMetric('echo "##kubesight-metric tool=eslint status=ok problems=0"', "eslint")).toBe(true);
    // Only the fallback line left: the counting line was deleted.
    expect(
      scriptKeepsMetric('echo "##kubesight-metric tool=eslint status=skipped problems=0"', "eslint")
    ).toBe(false);
    expect(scriptKeepsMetric('echo "##kubesight-metric tool=ruff status=ok"', "eslint")).toBe(false);
    expect(scriptKeepsMetric("npx eslint .", "eslint")).toBe(false);
  });
});
