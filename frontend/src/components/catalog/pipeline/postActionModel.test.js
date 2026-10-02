import { describe, expect, it } from "vitest";

import {
  blankPostAction,
  changePostType,
  moveAction,
  parseRecipients,
  postActionChange,
  postActionProblems,
  postActionsChanged,
  postActionsForApi,
  postActionsSentence,
  postActionSummary,
  postActionTitle,
  postRunVerdict,
  whenLabel,
  whenOptions,
  withPostKey,
} from "./postActionModel.js";

const secrets = [{ key: "SLACK_URL", scope: "service" }, { key: "TOKEN", scope: "global" }];

describe("when options", () => {
  it("offers fixed to notifications only", () => {
    expect(whenOptions("email").map((o) => o.value)).toEqual(["always", "success", "failure", "fixed"]);
    expect(whenOptions("webhook").map((o) => o.value)).toContain("fixed");
    expect(whenOptions("commands").map((o) => o.value)).toEqual(["always", "success", "failure"]);
    expect(whenLabel("failure")).toBe("on failure");
    expect(whenLabel(undefined)).toBe("always");
  });
});

describe("blanks and kind changes", () => {
  it("starts each kind in a sensible state", () => {
    expect(blankPostAction("email")).toMatchObject({ type: "email", when: "failure", recipients: [] });
    expect(blankPostAction("webhook")).toMatchObject({ type: "webhook", format: "slack", urlSecret: "" });
    expect(blankPostAction("commands")).toMatchObject({ type: "commands", when: "always", timeoutSeconds: 600 });
  });

  it("keeps the identity and when, drops the other kind's fields, and never leaves a cleanup on fixed", () => {
    const email = withPostKey({ ...blankPostAction("email"), when: "fixed", recipients: ["a@b.co"] });
    const cleanup = changePostType(email, "commands");
    expect(cleanup._key).toBe(email._key);
    expect(cleanup.when).toBe("always");
    expect(cleanup.recipients).toBeUndefined();
    const hook = changePostType({ ...email, when: "success" }, "webhook");
    expect(hook).toMatchObject({ type: "webhook", when: "success", urlSecret: "" });
  });
});

describe("titles and summaries", () => {
  it("names a row the way the build will", () => {
    expect(postActionTitle({ type: "email" })).toBe("Email");
    expect(postActionTitle({ type: "webhook", format: "teams" })).toBe("Teams");
    expect(postActionTitle({ type: "webhook", format: "json" })).toBe("Webhook");
    expect(postActionTitle({ type: "commands", name: "  " })).toBe("Cleanup");
    expect(postActionTitle({ type: "commands", name: "Release lock" })).toBe("Release lock");
  });

  it("says what each one does in a line", () => {
    expect(postActionSummary({ type: "email", recipients: [] })).toBe("No recipients yet");
    expect(postActionSummary({ type: "email", recipients: ["a@b.co", "c@d.co", "e@f.co"] })).toBe("a@b.co and 2 more");
    expect(postActionSummary({ type: "webhook", urlSecret: "SLACK_URL" })).toBe("URL from secret SLACK_URL");
    expect(postActionSummary({ type: "commands", commands: ["", "rm -rf tmp", "echo done"] })).toBe("rm -rf tmp · +1 more");
    expect(postActionsSentence([{ type: "email" }, { type: "webhook" }, { type: "commands" }])).toBe(
      "2 notifications · 1 cleanup"
    );
    expect(postActionsSentence([])).toBe("Nothing happens yet");
  });

  it("splits pasted recipients like the server", () => {
    expect(parseRecipients("a@b.co, c@d.co;A@B.co  e@f.co")).toEqual(["a@b.co", "c@d.co", "e@f.co"]);
  });
});

describe("problems mirror the server's rules", () => {
  const problemsOf = (action, list = [action]) =>
    postActionProblems(action, list.indexOf(action), list, secrets).map((p) => p.field);

  it("needs recipients that are addresses, at most 25", () => {
    expect(problemsOf({ type: "email", recipients: [] })).toEqual(["recipients"]);
    expect(problemsOf({ type: "email", recipients: ["not-an-address"] })).toEqual(["recipients"]);
    const many = Array.from({ length: 26 }, (_, i) => `u${i}@example.com`);
    expect(problemsOf({ type: "email", recipients: many })).toEqual(["recipients"]);
    expect(problemsOf({ type: "email", recipients: ["ok@example.com"] })).toEqual([]);
  });

  it("needs a webhook secret that exists", () => {
    expect(problemsOf({ type: "webhook", format: "slack", urlSecret: "" })).toEqual(["urlSecret"]);
    expect(problemsOf({ type: "webhook", format: "slack", urlSecret: "GONE" })).toEqual(["urlSecret"]);
    expect(problemsOf({ type: "webhook", format: "slack", urlSecret: "SLACK_URL" })).toEqual([]);
  });

  it("holds cleanup to commands, a time limit, unique names and real secrets", () => {
    expect(problemsOf({ type: "commands", commands: ["  "] })).toEqual(["commands"]);
    expect(problemsOf({ type: "commands", commands: ["x"], timeoutSeconds: 5 })).toEqual(["timeoutSeconds"]);
    expect(problemsOf({ type: "commands", commands: ["x"], timeoutSeconds: 3600 })).toEqual(["timeoutSeconds"]);
    expect(problemsOf({ type: "commands", commands: ["x"], when: "fixed" })).toEqual(["when"]);
    expect(problemsOf({ type: "commands", commands: ["x"], secretRefs: [{ name: "NOPE" }] })).toEqual(["secretRefs"]);
    const a = { type: "commands", name: "Tidy", commands: ["x"] };
    const b = { type: "commands", name: "tidy", commands: ["y"] };
    expect(problemsOf(a, [a, b])).toEqual(["name"]);
    expect(problemsOf({ type: "sms" })).toEqual(["type"]);
  });
});

describe("api shape and change tracking", () => {
  it("sends only the kind's fields, without the editor identity", () => {
    const list = [
      withPostKey({ ...blankPostAction("email"), recipients: ["a@b.co"], extra: 1 }),
      withPostKey({ ...blankPostAction("webhook"), urlSecret: "SLACK_URL" }),
      withPostKey({ ...blankPostAction("commands"), commands: ["rm -rf tmp"], secretRefs: [{ name: "TOKEN" }], timeoutSeconds: "" }),
    ];
    const sent = postActionsForApi(list);
    expect(sent[0]).toEqual({ type: "email", when: "failure", recipients: ["a@b.co"], subject: "", message: "" });
    expect(sent[1]).toEqual({ type: "webhook", when: "failure", format: "slack", urlSecret: "SLACK_URL" });
    expect(sent[2]).toMatchObject({
      type: "commands",
      image: null,
      workingDirectory: null,
      secretRefs: [{ name: "TOKEN", envVar: "TOKEN" }],
      timeoutSeconds: 600,
    });
    expect(sent.some((item) => "_key" in item)).toBe(false);
  });

  it("does not call a whitespace-only difference a change", () => {
    const saved = [withPostKey({ ...blankPostAction("commands"), commands: ["rm -rf tmp"] })];
    const current = [{ ...saved[0], commands: ["", "rm -rf tmp   ", ""] }];
    expect(postActionsChanged(saved, current)).toBe(false);
    expect(postActionsChanged(saved, [{ ...saved[0], when: "failure" }])).toBe(true);
    expect(postActionsChanged(saved, [])).toBe(true);
    expect(postActionChange(current[0], saved)).toBeNull();
    expect(postActionChange({ ...saved[0], when: "failure" }, saved)).toBe("edited");
    expect(postActionChange(withPostKey(blankPostAction("email")), saved)).toBe("new");
  });

  it("moves an action without losing any", () => {
    expect(moveAction(["a", "b", "c"], 0, 2)).toEqual(["b", "c", "a"]);
    expect(moveAction(["a", "b"], 0, 5)).toEqual(["a", "b"]);
  });
});

describe("what a build's post-action row says", () => {
  it("reads the row's state in words", () => {
    expect(postRunVerdict({ type: "email", status: "success", detail: "Email sent to a@b.co." })).toBe(
      "Email sent to a@b.co."
    );
    expect(postRunVerdict({ type: "webhook", status: "pending", phase: "queued", attempts: 1 })).toBe(
      "Retrying shortly."
    );
    expect(postRunVerdict({ type: "email", status: "pending", phase: "waiting" })).toBe("Waits for the build to end.");
    expect(postRunVerdict({ type: "email", status: "skipped", detail: "Not sent: this build succeeded." })).toBe(
      "Not sent: this build succeeded."
    );
    expect(postRunVerdict({ type: "commands", status: "failed", error: "The cleanup commands failed." })).toBe(
      "The cleanup commands failed."
    );
  });
});
