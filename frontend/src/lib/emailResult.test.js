import { describe, expect, it } from "vitest";
import { describeEmailResult } from "./emailResult";

describe("describeEmailResult", () => {
  it("says queued emails are queued, never that anyone was notified", () => {
    const result = describeEmailResult({ status: "queued", queued: 3, sent: 3, recipients: [] });
    expect(result.kind).toBe("queued");
    expect(result.text).toBe("approval emails to 3 recipients are queued and go out in the background");
    expect(result.text).not.toMatch(/notified/);
  });

  it("handles the change-bundle shape (queued count, sent 0) and one recipient", () => {
    expect(describeEmailResult({ sent: 0, queued: 1 }).text).toBe(
      "approval emails to 1 recipient are queued and go out in the background",
    );
  });

  it("reports synchronous sends, skips and failures", () => {
    expect(describeEmailResult({ sent: 2, errors: [] })).toEqual({
      kind: "sent",
      text: "2 recipients notified by email",
    });
    expect(describeEmailResult({ sent: 0, skipped: true, reason: "SMTP not configured." }).kind).toBe("skipped");
    expect(describeEmailResult({ sent: 0, errors: ["a@b: refused"] }).kind).toBe("failed");
    expect(describeEmailResult(null).kind).toBe("none");
  });
});
