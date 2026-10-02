import { describe, expect, it } from "vitest";
import {
  DEFAULT_APPROVAL_TIMEOUT_SECONDS,
  approvalOutcome,
  approvalProblems,
  approvalsSoFar,
  blankApproval,
  describeApprovers,
  timeLeft,
} from "./approvalModel.js";
import { blankDeploy, isServerStage, orderProblem } from "./deployModel.js";
import {
  blankStage,
  changeKindPatch,
  defaultStageName,
  fieldsLostOnKindChange,
  kindOf,
  stageProblems,
  stageSummary,
} from "./stageModel.js";
import {
  blankStoreUpload,
  defaultAppId,
  patchStoreUpload,
  storeUploadOutcome,
  storeUploadProblems,
  storeUploadSummary,
  targetLabel,
} from "./storeUploadModel.js";

const command = (name = "Build") => ({ ...blankStage("command"), name, commands: ["make"] });
const approval = (extra = {}) => ({ ...blankStage("approval"), name: "Sign-off", approval: { ...blankApproval(), ...extra } });
const upload = (extra = {}) => ({ ...blankStage("store_upload"), name: "To Play", storeUpload: { ...blankStoreUpload(3), ...extra } });

describe("the Approval kind", () => {
  it("is offered with its own verb, icon and name", () => {
    expect(kindOf("approval")).toMatchObject({ verb: "Wait for approval", icon: "approval" });
    expect(defaultStageName("approval")).toBe("Approval");
  });

  it("arrives asking permission holders for one approval, and waits hours not minutes", () => {
    const stage = blankStage("approval");
    expect(stage.approval).toMatchObject({ anyoneWithPermission: true, minApprovals: 1, allowSelfApproval: false });
    expect(stage.timeoutSeconds).toBe(DEFAULT_APPROVAL_TIMEOUT_SECONDS);
    const patch = changeKindPatch("approval");
    expect(patch.approval.minApprovals).toBe(1);
    expect(patch.timeoutSeconds).toBe(DEFAULT_APPROVAL_TIMEOUT_SECONDS);
    // A server stage keeps no image, commands or files.
    expect(patch.image).toBe("");
    expect(patch.commands).toEqual([]);
    expect(patch.artifacts).toEqual([]);
  });

  it("refuses a stage nobody could pass", () => {
    expect(approvalProblems({ ...blankApproval(), anyoneWithPermission: false })[0].message).toContain("Nobody may approve");
    const named = { ...blankApproval(), anyoneWithPermission: false, users: [{ id: 1, username: "alice" }], minApprovals: 2 };
    expect(approvalProblems(named)[0].message).toContain("could never pass");
    expect(approvalProblems({ ...blankApproval(), minApprovals: 0 })[0].message).toContain("from 1 to 10");
    expect(approvalProblems(blankApproval())).toEqual([]);
    expect(stageProblems(approval({ anyoneWithPermission: false }), 0, [], []).some((p) => p.field === "approval")).toBe(true);
  });

  it("says who may approve in one line", () => {
    expect(describeApprovers({ users: [{ id: 1, username: "alice" }], anyoneWithPermission: true })).toBe(
      "alice or anyone with ci_builds:approve"
    );
    expect(stageSummary(approval({ minApprovals: 2 }))).toBe("Waits for 2 approvals from anyone with ci_builds:approve");
  });

  it("counts distinct approvers and reads the wait", () => {
    const state = {
      phase: "waiting_approval",
      required: 2,
      decisions: [
        { userId: 1, username: "a", decision: "approve" },
        { userId: 1, username: "a", decision: "approve" },
        { userId: 2, username: "b", decision: "reject" },
      ],
    };
    expect(approvalsSoFar(state).map((d) => d.username)).toEqual(["a"]);
    expect(approvalOutcome(state)).toEqual({ tone: "warn", label: "Waiting for approval · 1 of 2" });
    expect(approvalOutcome({ outcome: "timed_out" }).label).toBe("Not approved in time");
    expect(approvalOutcome({ outcome: "rejected" }).tone).toBe("error");
  });

  it("counts down to the deadline", () => {
    const now = Date.parse("2026-10-02T10:00:00Z");
    expect(timeLeft("2026-10-02T13:20:00+00:00", now)).toBe("in 3 h 20 min");
    expect(timeLeft("2026-10-02T10:45:00+00:00", now)).toBe("in 45 min");
    expect(timeLeft("2026-10-02T09:00:00+00:00", now)).toBe("any moment");
    expect(timeLeft("", now)).toBe("");
  });

  it("drops its approvers when it becomes another kind, and says so", () => {
    const stage = approval({ users: [{ id: 4, username: "dana" }] });
    expect(fieldsLostOnKindChange(stage, "command")).toContain("approvers");
    expect(changeKindPatch("command").approval).toBeNull();
  });
});

describe("the App store upload kind", () => {
  it("is offered with its own verb, icon and name", () => {
    expect(kindOf("store_upload")).toMatchObject({ verb: "Publish to an app store", icon: "store" });
    expect(defaultStageName("store_upload")).toBe("Publish to store");
    expect(changeKindPatch("store_upload").storeUpload).toMatchObject({ store: "google_play", target: "internal", artifactType: "aab" });
  });

  it("defaults the app to the one linked to the service, only when it is the only one", () => {
    const apps = [{ id: 1, ciServiceId: 9 }, { id: 2, ciServiceId: 5 }];
    expect(defaultAppId(apps, 9)).toBe(1);
    expect(defaultAppId([...apps, { id: 3, ciServiceId: 9 }], 9)).toBeNull();
    expect(defaultAppId(apps, 7)).toBeNull();
  });

  it("switching store resets the track and file to that store's", () => {
    const next = patchStoreUpload(blankStoreUpload(1), { store: "app_store" });
    expect(next).toMatchObject({ store: "app_store", target: "testflight", artifactType: "ipa", appId: 1 });
    expect(patchStoreUpload(next, { target: "review" }).artifactType).toBe("ipa");
  });

  it("refuses what the store would not take", () => {
    expect(storeUploadProblems({ ...blankStoreUpload(), store: "app_store", target: "internal", artifactType: "ipa" })[0].message).toContain("TestFlight");
    expect(storeUploadProblems({ ...blankStoreUpload(), artifactType: "ipa" })[0].message).toContain("does not take IPA");
    expect(storeUploadProblems({ ...blankStoreUpload(), artifactPattern: "$(rm)" })[0].message).toContain("file pattern");
    expect(storeUploadProblems(blankStoreUpload())).toEqual([]);
    expect(stageProblems(upload({ artifactType: "ipa" }), 0, [], []).some((p) => p.field === "storeUpload")).toBe(true);
  });

  it("reads the target and the publish in words", () => {
    expect(targetLabel(blankStoreUpload())).toBe("Google Play (internal track)");
    expect(targetLabel({ store: "app_store", target: "testflight" })).toBe("App Store Connect (TestFlight)");
    expect(storeUploadSummary(upload(), [{ id: 3, name: "POS" }])).toBe("POS → Google Play (internal track)");
    expect(storeUploadOutcome({ phase: "publishing", publishStatus: "processing" }).label).toBe("Store is processing");
    expect(storeUploadOutcome({ outcome: "published" }).tone).toBe("success");
  });
});

describe("server stages come last", () => {
  it("knows which kinds run on the server", () => {
    expect(["deploy", "approval", "store_upload"].every((kind) => isServerStage({ stageType: kind }))).toBe(true);
    expect(isServerStage({ stageType: "command" })).toBe(false);
  });

  it("puts the problem on a runner stage after an Approval", () => {
    const stages = [approval(), command("Smoke test")];
    expect(orderProblem(stages[1], 1, stages).message).toContain("Comes after the Approval stage “Sign-off”");
    expect(orderProblem(stages[0], 0, stages)).toBeNull();
  });

  it("lets an Approval sit before a Deploy and a store upload", () => {
    const deploy = { ...blankStage("deploy"), name: "Deploy", deploy: blankDeploy() };
    const stages = [command(), approval(), deploy, upload()];
    stages.forEach((stage, index) => expect(orderProblem(stage, index, stages)).toBeNull());
  });

  it("names the store upload kind when a runner stage follows it", () => {
    const stages = [upload(), command("After")];
    expect(orderProblem(stages[1], 1, stages).message).toContain("App store upload stage “To Play”");
  });
});
