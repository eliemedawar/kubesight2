import { describe, expect, it } from "vitest";

import {
  blankWebhook,
  buildsWhat,
  curlExample,
  formProblems,
  outcomeOf,
  railSummary,
  rejectedRecently,
  repositoryName,
  requestChoosesRef,
  sampleBody,
  samplePush,
  toForm,
  toPayload,
} from "./webhookModel.js";

const PARAMS = [
  { name: "VERSION", type: "text", default: "" },
  { name: "TARGET", type: "choice", choices: ["uat", "prod"], default: "uat" },
  { name: "SCAN", type: "boolean", default: "false" },
];

describe("blank and round trip", () => {
  it("starts a push webhook filtered to the default branch", () => {
    expect(blankWebhook("bitbucket_push", "develop").branchFilters).toEqual(["develop"]);
    expect(blankWebhook("generic", "develop").branchFilters).toEqual([]);
  });

  it("round-trips an existing webhook", () => {
    const hook = {
      id: 3,
      kind: "generic",
      name: "Release",
      pipelineId: 9,
      refType: "tag",
      branch: "v1",
      variables: { TARGET: "prod" },
      allowedInputs: ["VERSION"],
      allowRefOverride: true,
      mappings: [{ target: "VERSION", path: "release.tag" }],
      branchFilters: [],
      tagFilters: ["v*"],
      enabled: false,
      skipIfRunning: true,
    };
    const form = toForm(hook);
    expect(form.pipelineId).toBe("9");
    const payload = toPayload(form, PARAMS);
    expect(payload).toMatchObject({
      name: "Release",
      pipelineId: 9,
      refType: "tag",
      branch: "v1",
      variables: { TARGET: "prod" },
      allowedInputs: ["VERSION"],
      allowRefOverride: true,
      mappings: [{ target: "VERSION", path: "release.tag" }],
      enabled: false,
      skipIfRunning: true,
    });
    // The kind is only sent on create: the server refuses changing it.
    expect(payload.kind).toBeUndefined();
  });
});

describe("toPayload", () => {
  it("drops values and allowed inputs the pipeline does not declare, and empty mapping rows", () => {
    const form = {
      ...blankWebhook("generic"),
      variables: { TARGET: "prod", GONE: "x" },
      allowedInputs: ["VERSION", "GONE"],
      mappings: [{ target: "", path: "" }, { target: "VERSION", path: "a.b" }],
    };
    const payload = toPayload(form, PARAMS);
    expect(payload.kind).toBe("generic");
    expect(payload.variables).toEqual({ TARGET: "prod" });
    expect(payload.allowedInputs).toEqual(["VERSION"]);
    expect(payload.mappings).toEqual([{ target: "VERSION", path: "a.b" }]);
  });

  it("keeps free-form allowed inputs when the pipeline declares none", () => {
    const payload = toPayload({ ...blankWebhook("generic"), allowedInputs: ["ANYTHING"] }, []);
    expect(payload.allowedInputs).toEqual(["ANYTHING"]);
  });

  it("sends a push webhook only what a push uses", () => {
    const payload = toPayload({ ...blankWebhook("bitbucket_push", "main"), buildTags: true, tagFilters: ["v*"] }, []);
    expect(payload).toMatchObject({ kind: "bitbucket_push", branchFilters: ["main"], buildTags: true, tagFilters: ["v*"] });
    expect(payload.mappings).toBeUndefined();
    expect(payload.allowRefOverride).toBeUndefined();
  });
});

describe("formProblems", () => {
  it("catches the obvious before the round trip", () => {
    expect(formProblems({ ...blankWebhook("generic"), name: " " }).name).toBeTruthy();
    expect(formProblems({ ...blankWebhook("generic"), refType: "tag", branch: "" }).branch).toBeTruthy();
    expect(formProblems({ ...blankWebhook("generic"), mappings: [{ target: "VERSION", path: "" }] }).mappings).toMatch(/both/);
    expect(
      formProblems({ ...blankWebhook("generic"), mappings: [{ target: "ref:branch", path: "a" }, { target: "ref:tag", path: "b" }] })
        .mappings
    ).toMatch(/not both/);
    expect(formProblems(blankWebhook("generic"))).toEqual({});
  });
});

describe("words", () => {
  it("says what a webhook builds", () => {
    expect(buildsWhat({ kind: "bitbucket_push", branchFilters: ["develop"], buildTags: false }, "main")).toBe("Pushes to develop");
    expect(buildsWhat({ kind: "bitbucket_push", branchFilters: [], buildTags: true, tagFilters: ["v*"] })).toBe(
      "Pushes to any branch, and tags v*"
    );
    expect(buildsWhat({ kind: "generic", refType: "branch", branch: "" }, "develop")).toBe("develop (default branch)");
    expect(buildsWhat({ kind: "generic", refType: "tag", branch: "v1", allowRefOverride: true })).toBe(
      "tag v1, or the ref the request names"
    );
  });

  it("knows when the request chooses the ref", () => {
    expect(requestChoosesRef(blankWebhook("generic"))).toBe(false);
    expect(requestChoosesRef({ ...blankWebhook("generic"), mappings: [{ target: "ref:tag", path: "ref" }] })).toBe(true);
    expect(requestChoosesRef(blankWebhook("bitbucket_push"))).toBe(true);
  });

  it("describes the last delivery", () => {
    expect(outcomeOf({}).label).toBe("No calls yet");
    expect(outcomeOf({ lastOutcome: "triggered", lastBuild: { number: 4, status: "success" } })).toMatchObject({
      tone: "ok",
      label: "Build #4 success",
    });
    expect(outcomeOf({ lastOutcome: "refused", lastMessage: "may not set X" })).toMatchObject({
      tone: "danger",
      detail: "may not set X",
    });
  });

  it("flags a wrong secret after the last good call", () => {
    expect(rejectedRecently({ lastRejectedAt: "2026-10-06T10:00:00Z", lastDeliveryAt: "2026-10-06T09:00:00Z" })).toBe(true);
    expect(rejectedRecently({ lastRejectedAt: "2026-10-06T08:00:00Z", lastDeliveryAt: "2026-10-06T09:00:00Z" })).toBe(false);
    expect(rejectedRecently({ lastRejectedAt: "2026-10-06T08:00:00Z" })).toBe(true);
    expect(rejectedRecently({})).toBe(false);
  });

  it("summarises for the rail", () => {
    expect(railSummary(null).text).toBe("");
    expect(railSummary([]).text).toBe("None");
    expect(railSummary([{ enabled: true }, { enabled: false }])).toEqual({ text: "1 active", tone: "" });
    expect(railSummary([{ enabled: true, lastOutcome: "refused" }]).tone).toBe("warn");
  });
});

describe("examples", () => {
  it("reads the repository out of a Bitbucket URL", () => {
    expect(repositoryName("https://bitbucket.org/Areeba/payment-service.git")).toBe("areeba/payment-service");
    expect(repositoryName("git@bitbucket.org:areeba/x.git")).toBe("areeba/x");
    expect(repositoryName("https://github.com/a/b")).toBe("");
  });

  it("builds a Bitbucket push body", () => {
    const body = samplePush({ repository: "a/b", branch: "develop" });
    expect(body.repository.full_name).toBe("a/b");
    expect(body.push.changes[0].new).toEqual({ type: "branch", name: "develop" });
  });

  it("builds a sample generic body from what the webhook allows", () => {
    const body = sampleBody(
      { allowRefOverride: true, refType: "branch", branchFilters: ["release/*", "main"], allowedInputs: ["VERSION", "TARGET", "SCAN"] },
      PARAMS
    );
    expect(body).toEqual({ branch: "main", variables: { VERSION: "1.0.0", TARGET: "uat", SCAN: true } });
    expect(sampleBody({ allowedInputs: [] }, PARAMS)).toEqual({});
  });

  it("writes a curl command that keeps the secret out unless revealed", () => {
    const hook = { url: "https://ks.example.com/api/ci/hooks/wh_x" };
    const hidden = curlExample(hook, { body: {} });
    expect(hidden).toContain("$KUBESIGHT_WEBHOOK_SECRET");
    expect(hidden).not.toContain("Content-Type");
    const shown = curlExample(hook, { secret: "s3cr'et", body: { branch: "main" } });
    expect(shown).toContain(`'X-KubeSight-Secret: s3cr'\\''et'`);
    expect(shown).toContain(`-d '{"branch":"main"}'`);
  });
});
