import { describe, expect, it } from "vitest";

import {
  blankStage,
  changeKindPatch,
  describeDiff,
  fieldsLostOnKindChange,
  firstCommand,
  lintByStage,
  mergePlainEnv,
  moveItem,
  parameterProblems,
  pipelineDiff,
  plainEnv,
  shortImage,
  stageFlags,
  stageProblems,
  stagesForLint,
  stagesUsingParameter,
  stageSummary,
  timeoutLabel,
  uniqueStageName,
  withKey,
} from "./stageModel.js";

const command = (name, commands = ["make"], extra = {}) => ({
  ...blankStage("command"),
  name,
  commands,
  ...extra,
});

describe("stage problems mirror what a save refuses", () => {
  it("wants a name, and a unique one regardless of case", () => {
    const stages = [command(""), command("Build"), command("build")];
    expect(stageProblems(stages[0], 0, stages, []).map((p) => p.field)).toEqual(["name"]);
    expect(stageProblems(stages[1], 1, stages, [])[0].message).toMatch(/unique/);
    expect(stageProblems(stages[2], 2, stages, [])[0].message).toMatch(/unique/);
  });

  it("refuses a command stage with no commands even when it is turned off", () => {
    // The backend validates every stage, enabled or not, so an "off" stage is
    // not an escape hatch for an empty one.
    const stage = command("Lint", ["", "   "], { enabled: false });
    expect(stageProblems(stage, 0, [stage], []).map((p) => p.field)).toEqual(["commands"]);
  });

  it("flags a timeout outside 30s–24h", () => {
    const stage = command("Slow", ["x"], { timeoutSeconds: 10 });
    expect(stageProblems(stage, 0, [stage], [])[0].field).toBe("timeout");
    expect(stageProblems({ ...stage, timeoutSeconds: 3600 }, 0, [stage], [])).toEqual([]);
  });

  it("warns — without blocking — about a condition on an input that does not exist", () => {
    const stage = command("Deploy", ["x"], {
      runCondition: { variable: "DEPLOY_ENV", operator: "equals", value: "prod" },
    });
    const [problem] = stageProblems(stage, 0, [stage], []);
    expect(problem.level).toBe("warning");
    expect(stageProblems(stage, 0, [stage], [{ name: "DEPLOY_ENV" }])).toEqual([]);
  });

  it("names retired kinds as unsaveable", () => {
    // `scan` left the retired list when it gained an executor (scanModel.test.js).
    const stage = { ...blankStage("publish_artifact"), name: "Publish" };
    expect(stageProblems(stage, 0, [stage], [])[0].field).toBe("kind");
  });
});

describe("parameter problems", () => {
  it("wants an environment-variable name, once", () => {
    const params = [{ name: "1BAD" }, { name: "OK" }, { name: "OK" }];
    expect(parameterProblems(params[0], 0, params)[0].message).toMatch(/Letters/);
    expect(parameterProblems(params[2], 2, params)[0].message).toMatch(/twice/);
  });

  it("wants a choice to have options and a default among them", () => {
    expect(parameterProblems({ name: "ENV", type: "choice", choices: [""] }, 0, [])[0].field).toBe("choices");
    expect(
      parameterProblems({ name: "ENV", type: "choice", choices: ["uat", "prod"], default: "dev" }, 0, [])[0].field
    ).toBe("default");
  });
});

describe("kind changes", () => {
  it("clears what the new kind would silently ignore", () => {
    const patch = changeKindPatch("checkout");
    expect(patch).toMatchObject({ stageType: "checkout", commands: [], image: "", env: {}, imageScan: null });
    expect(patch).not.toHaveProperty("hostAliases"); // checkout still uses it
  });

  it("lists only fields that hold something", () => {
    const stage = command("Build", ["mvn package"], { image: "maven:3", env: {} });
    expect(fieldsLostOnKindChange(stage, "container_image")).toEqual(["container image", "commands"]);
    expect(fieldsLostOnKindChange(command("Empty", []), "checkout")).toEqual([]);
  });
});

describe("change tracking against the saved pipeline", () => {
  const saved = {
    stages: [
      { ...command("Checkout"), id: 1, stageType: "checkout", commands: [] },
      { ...command("Build", ["mvn package"]), id: 2, timeoutSeconds: 1800 },
    ],
    parameters: [],
  };

  it("sees nothing when nothing changed, even with a timeout typed as a string", () => {
    const stages = saved.stages.map((stage) => ({ ...stage, timeoutSeconds: String(stage.timeoutSeconds) }));
    expect(pipelineDiff(stages, [], saved).count).toBe(0);
  });

  it("marks edited, new, removed and reordered separately", () => {
    const stages = [
      { ...saved.stages[1], commands: ["mvn verify"] },
      saved.stages[0],
      command("Test", ["mvn test"]),
    ];
    const diff = pipelineDiff(stages, [], saved);
    expect(diff.perStage).toEqual(["edited", null, "new"]);
    expect(diff).toMatchObject({ edited: 1, added: 1, removed: 0, reordered: true });
    expect(describeDiff(diff)).toBe("1 stage edited, 1 stage added, order changed");
    expect(pipelineDiff([saved.stages[0]], [], saved).removed).toBe(1);
  });

  it("does not call a starter pipeline's id-less stages changed the moment it loads", () => {
    // Generated stages have no database id; the editor's client key is what
    // ties a draft stage to its saved self.
    const generated = [command("Checkout", []), command("Build", ["mvn package"])].map(withKey);
    const draft = generated.map((stage) => ({ ...stage }));
    expect(pipelineDiff(draft, [], { stages: generated, parameters: [] }).count).toBe(0);
    draft[1] = { ...draft[1], commands: ["mvn verify"] };
    expect(pipelineDiff(draft, [], { stages: generated, parameters: [] }).perStage).toEqual([null, "edited"]);
  });

  it("stops calling a value typed back to what it was a change", () => {
    const stages = [saved.stages[0], { ...saved.stages[1], commands: ["mvn package"] }];
    expect(pipelineDiff(stages, [], saved).count).toBe(0);
  });
});

describe("lint findings land on the stage they are about", () => {
  it("numbers stages by where they are now, not where they were saved", () => {
    // A saved stage carries its stored 0-based position; after a reorder that
    // number points at a different stage.
    const stages = [
      { ...command("B"), id: 2, position: 1 },
      { ...command("A"), id: 1, position: 0 },
      command("New"),
    ];
    expect(stagesForLint(stages).map((stage) => stage.position)).toEqual([1, 2, 3]);
    const lint = {
      findings: [
        { stagePosition: 3, level: "error", message: "abs path" },
        { stagePosition: 1, level: "info", message: "noise" },
      ],
    };
    const map = lintByStage(lint);
    expect([...map.keys()]).toEqual([2]);
  });
});

describe("one-line summaries", () => {
  it("drops the registry host from an image", () => {
    expect(shortImage("registry.areeba.com/alpine/git:2.45")).toBe("alpine/git:2.45");
    expect(shortImage("localhost/tool")).toBe("tool");
    expect(shortImage("node:20")).toBe("node:20");
    expect(shortImage("library/node:20")).toBe("library/node:20");
  });

  it("reads the first real command, skipping shell preamble", () => {
    expect(firstCommand({ commands: ["set -e", "# build it", "", "npm ci"] })).toBe("npm ci");
  });

  it("says what an image stage builds", () => {
    const stage = { ...blankStage("container_image"), env: { DOCKERFILE_PATH: "ops/Dockerfile" }, workingDirectory: "app" };
    expect(stageSummary(stage)).toBe("Builds ops/Dockerfile in app");
  });

  it("formats timeouts people read", () => {
    expect(timeoutLabel(1800)).toBe("30 min");
    expect(timeoutLabel(5400)).toBe("1 h 30 min");
    expect(timeoutLabel(45)).toBe("45s");
  });

  it("marks what changes a stage's behaviour", () => {
    const stage = command("Deploy", ["x"], {
      enabled: false,
      continueOnFailure: true,
      runCondition: { variable: "ENV", operator: "equals", value: "prod" },
    });
    expect(stageFlags(stage).map((flag) => flag.key)).toEqual(["off", "when", "continue"]);
  });
});

describe("image build inputs live in env but are edited as fields", () => {
  const stage = {
    ...blankStage("container_image"),
    env: { IMAGE_TAG: "v1", HTTP_PROXY: "p", DOCKERFILE_PATH: "Dockerfile.ci" },
  };

  it("keeps them out of the generic variable list", () => {
    expect(plainEnv(stage)).toEqual({ HTTP_PROXY: "p" });
  });

  it("does not lose them when the generic list is edited", () => {
    expect(mergePlainEnv(stage, { NO_PROXY: "x" })).toEqual({
      NO_PROXY: "x",
      IMAGE_TAG: "v1",
      DOCKERFILE_PATH: "Dockerfile.ci",
    });
  });
});

describe("helpers", () => {
  it("finds a free name", () => {
    expect(uniqueStageName("Build", [command("Build"), command("build 2")])).toBe("Build 3");
    expect(uniqueStageName("Build", [command("Build")], 0)).toBe("Build");
  });

  it("moves an item and leaves out-of-range moves alone", () => {
    expect(moveItem(["a", "b", "c"], 0, 2)).toEqual(["b", "c", "a"]);
    const items = ["a"];
    expect(moveItem(items, 0, 3)).toBe(items);
  });

  it("finds the stages an input steers", () => {
    const stages = [command("A"), command("B", ["x"], { runCondition: { variable: "ENV" } })];
    expect(stagesUsingParameter("ENV", stages).map(({ index }) => index)).toEqual([1]);
  });
});
