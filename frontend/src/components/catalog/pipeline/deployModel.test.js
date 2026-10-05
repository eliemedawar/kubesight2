import { describe, expect, it } from "vitest";

import {
  blankDeploy,
  deployOutcome,
  deployProblems,
  deployTarget,
  isLinkedDeploy,
  pickLinkedDeployment,
  setDeployTargetMode,
  generateManifest,
  orderProblem,
  patchDeploy,
} from "./deployModel.js";
import { blankStage, changeKindPatch, fieldsLostOnKindChange, stageProblems, stageSummary } from "./stageModel.js";

// The form path; new stages default to an inventory template (tested below).
const target = (extra = {}) => ({
  ...blankDeploy(),
  create: { ...blankDeploy().create, source: "form" },
  clusterId: "prod",
  namespace: "payments",
  deploymentName: "payments-api",
  ...extra,
});

const deployStage = (deploy = target()) => ({ ...blankStage("deploy"), name: "Deploy", deploy });

describe("generateManifest", () => {
  it("names the deployment, container and selector after the target", () => {
    const text = generateManifest(target());
    expect(text).toContain("kind: Deployment");
    expect(text).toContain("  name: payments-api");
    expect(text).toContain("        - name: payments-api");
    expect(text).toContain('image: "${IMAGE}"');
    expect(text).toContain("app: payments-api");
  });

  it("adds a Service only when asked and when there is a port", () => {
    expect(generateManifest(target())).toContain("kind: Service");
    const noService = target({ create: { ...blankDeploy().create, service: { enabled: false } } });
    expect(generateManifest(noService)).not.toContain("kind: Service");
    const noPort = target({ create: { ...blankDeploy().create, port: null } });
    expect(generateManifest(noPort)).not.toContain("kind: Service");
  });

  it("quotes env values so any text stays a string", () => {
    const deploy = target({ create: { ...blankDeploy().create, env: { MODE: "yes: no", N: "007" } } });
    const text = generateManifest(deploy);
    expect(text).toContain('value: "yes: no"');
    expect(text).toContain('value: "007"');
  });

  it("is deterministic, so an untouched stage never reads as edited", () => {
    expect(generateManifest(target())).toBe(generateManifest(target()));
  });
});

describe("patchDeploy", () => {
  it("keeps a generated manifest in step with the form", () => {
    const next = patchDeploy(target(), { deploymentName: "ledger" });
    expect(next.manifest).toContain("name: ledger");
    const bigger = patchDeploy(next, { create: { replicas: 3 } });
    expect(bigger.manifest).toContain("replicas: 3");
    expect(bigger.create.port).toBe(8080); // The rest of the form is kept.
  });

  it("leaves a hand-edited manifest alone", () => {
    const custom = target({ manifest: "kind: Deployment # mine", create: { ...blankDeploy().create, customManifest: true } });
    expect(patchDeploy(custom, { deploymentName: "other" }).manifest).toBe("kind: Deployment # mine");
  });
});

describe("deployProblems", () => {
  it("asks for every part of the target", () => {
    const messages = deployProblems(blankDeploy()).map((item) => item.message);
    expect(messages).toEqual(
      expect.arrayContaining(["Pick a cluster to deploy to.", "Pick a namespace.", "Pick a deployment, or name the one to create."])
    );
  });

  it("holds names and quantities to the Kubernetes rules", () => {
    const bad = target({ namespace: "Payments", create: { ...blankDeploy().create, source: "form", memoryLimit: "lots" } });
    const messages = deployProblems(bad).map((item) => item.message).join(" ");
    expect(messages).toContain("namespace must be lowercase");
    expect(messages).toContain("“lots” is not a Kubernetes quantity");
  });

  it("is quiet for a complete target", () => {
    expect(deployProblems(target())).toEqual([]);
  });
});

describe("Deploy stages come last", () => {
  it("flags a stage placed after a Deploy stage, on that stage", () => {
    const stages = [deployStage(), { ...blankStage("command"), name: "Smoke test", commands: ["curl"] }];
    expect(orderProblem(stages[0], 0, stages)).toBeNull();
    expect(orderProblem(stages[1], 1, stages).message).toContain("Comes after the Deploy stage “Deploy”");
    expect(stageProblems(stages[1], 1, stages, []).some((item) => item.field === "kind")).toBe(true);
  });

  it("allows several Deploy stages in a row", () => {
    const stages = [deployStage(), { ...deployStage(target({ namespace: "uat" })), name: "Deploy UAT" }];
    expect(orderProblem(stages[1], 1, stages)).toBeNull();
  });
});

describe("the Deploy kind in the stage model", () => {
  it("arrives with a blank target and a generated manifest", () => {
    const stage = blankStage("deploy");
    expect(stage.deploy.createIfMissing).toBe(true);
    expect(stage.deploy.manifest).toContain("kind: Deployment");
    expect(changeKindPatch("deploy").deploy.clusterId).toBe("");
  });

  it("drops the target when the stage becomes another kind, and says so", () => {
    expect(changeKindPatch("command").deploy).toBeNull();
    expect(fieldsLostOnKindChange(deployStage(), "command")).toContain("deployment target");
  });

  it("reads as its target", () => {
    expect(stageSummary(deployStage())).toBe("→ prod / payments / payments-api");
    expect(stageSummary(blankStage("deploy"))).toBe("No target picked yet");
  });
});

describe("deployOutcome", () => {
  it("names what happened", () => {
    expect(deployOutcome({ outcome: "deployed" }).label).toBe("Deployed");
    expect(deployOutcome({ outcome: "rolled_back" }).tone).toBe("error");
    expect(deployOutcome({ phase: "waiting_approval" }).label).toBe("Waiting for approval");
    expect(deployOutcome(null)).toBeNull();
  });
});

describe("linked deploy targets", () => {
  const linked = (extra = {}) => ({ ...setDeployTargetMode(target(), "linked"), ...extra });

  it("switching to linked keeps the picked target for switching back", () => {
    const next = setDeployTargetMode(target(), "linked");
    expect(isLinkedDeploy(next)).toBe(true);
    expect(next.createIfMissing).toBe(false);
    const back = setDeployTargetMode(next, "fixed");
    expect(isLinkedDeploy(back)).toBe(false);
    expect(back.deploymentName).toBe("payments-api");
  });

  it("needs no cluster, namespace or deployment", () => {
    expect(deployProblems({ ...linked(), clusterId: "", namespace: "", deploymentName: "" })).toEqual([]);
    expect(deployProblems(linked({ environment: "<prod>" }))[0].message).toMatch(/environment label/);
  });

  it("reads as the service's linked deployment", () => {
    expect(deployTarget(linked())).toBe("the service's linked deployment");
    expect(deployTarget(linked({ environment: "PROD" }))).toBe("the service's linked deployment (PROD)");
  });

  it("picks the link the backend would", () => {
    const prod = { id: 1, environment: "PROD" };
    const uat = { id: 2, environment: "UAT" };
    expect(pickLinkedDeployment([prod], "").link).toBe(prod);
    expect(pickLinkedDeployment([prod, uat], "").problem).toMatch(/2 deployments/);
    expect(pickLinkedDeployment([prod, uat], "uat").link).toBe(uat);
    expect(pickLinkedDeployment([prod, uat], "SIT").problem).toMatch(/No linked deployment/);
    expect(pickLinkedDeployment([], "").problem).toMatch(/not linked/);
  });
});

describe("creating from an inventory template", () => {
  it("is what a new stage starts with", () => {
    expect(blankDeploy().create.source).toBe("template");
  });

  it("needs a template, and then no manifest", () => {
    const deploy = target({ create: { ...blankDeploy().create, source: "template", templateId: "" }, manifest: "" });
    expect(deployProblems(deploy).map((p) => p.message)).toEqual([
      "Pick the inventory template to create the deployment from.",
    ]);
    expect(deployProblems({ ...deploy, create: { ...deploy.create, templateId: "acquiring-ui" } })).toEqual([]);
  });
});
