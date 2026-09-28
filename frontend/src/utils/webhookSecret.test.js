import { describe, expect, it } from "vitest";
import { generateWebhookSecret, inboundWebhookState } from "./webhookSecret.js";

describe("generateWebhookSecret", () => {
  it("returns a URL-safe secret of the requested length", () => {
    const secret = generateWebhookSecret(40);
    expect(secret).toHaveLength(40);
    expect(secret).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("does not repeat itself", () => {
    expect(generateWebhookSecret()).not.toEqual(generateWebhookSecret());
  });

  it("refuses to fall back to weak randomness", () => {
    expect(() => generateWebhookSecret(40, {})).toThrow(/Secure random/);
  });
});

describe("inboundWebhookState", () => {
  it("says webhooks are rejected when no secret is set", () => {
    const state = inboundWebhookState(false);
    expect(state.tone).toBe("warn");
    expect(state.detail).toMatch(/webhooks are rejected/);
  });

  it("is quiet once a secret is configured", () => {
    expect(inboundWebhookState(true)).toEqual({ tone: "ok", label: "Secret configured" });
  });
});
