import { describe, expect, it } from "vitest";
import { secretValuesHiddenNote, temporaryPasswordNotice } from "./sensitiveResponses.js";

describe("secretValuesHiddenNote", () => {
  it("is empty when values are shown", () => {
    expect(secretValuesHiddenNote({ yaml: "x" })).toBe("");
    expect(secretValuesHiddenNote({ valuesHidden: false })).toBe("");
    expect(secretValuesHiddenNote(null)).toBe("");
  });

  it("names the hidden keys and the permission", () => {
    const note = secretValuesHiddenNote({
      valuesHidden: true,
      hiddenKeys: ["password", "username"],
      revealPermission: "secrets:reveal",
    });
    expect(note).toContain("password, username");
    expect(note).toContain("secrets:reveal");
  });
});

describe("temporaryPasswordNotice", () => {
  it("shows a revealed password once", () => {
    const notice = temporaryPasswordNotice({ username: "a", temporaryPassword: "Tmp!1" }, "created");
    expect(notice.password).toBe("Tmp!1");
    expect(notice.tone).toBe("warn");
  });

  it("reports a failed delivery without a password", () => {
    const notice = temporaryPasswordNotice(
      {
        username: "a",
        temporaryPasswordEmailed: false,
        temporaryPasswordDeliveryFailed: true,
        temporaryPasswordRevealed: false,
        temporaryPasswordHint: "Fix SMTP then resend.",
        temporaryPasswordDeliveryError: "Could not reach SMTP server",
      },
      "created"
    );
    expect(notice.tone).toBe("error");
    expect(notice.password).toBeUndefined();
    expect(notice.detail).toBe("Fix SMTP then resend.");
    expect(notice.error).toContain("SMTP");
  });

  it("confirms an emailed password", () => {
    expect(temporaryPasswordNotice({ temporaryPasswordEmailed: true }, "updated").tone).toBe("ok");
  });
});
