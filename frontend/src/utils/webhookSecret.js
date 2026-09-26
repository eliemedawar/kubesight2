// Shared secrets for inbound webhooks, generated in the browser.
//
// The backend never returns a stored secret, so the operator has to see the
// value once — here, before saving — to paste it into the sender (Zoho Desk
// workflow, Jira webhook). URL-safe so it also survives the ?secret= fallback.

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

export function generateWebhookSecret(length = 40, cryptoImpl = globalThis.crypto) {
  if (!cryptoImpl?.getRandomValues) {
    throw new Error("Secure random numbers are not available in this browser.");
  }
  const bytes = new Uint8Array(length);
  cryptoImpl.getRandomValues(bytes);
  // 256 is a multiple of 64, so `% 64` introduces no bias.
  return Array.from(bytes, (b) => ALPHABET[b % ALPHABET.length]).join("");
}

/** The pill/banner text for an integration's inbound webhook state. */
export function inboundWebhookState(secretConfigured) {
  return secretConfigured
    ? { tone: "ok", label: "Secret configured" }
    : {
        tone: "warn",
        label: "Rejecting webhooks — no secret",
        detail:
          "Inbound secret not configured — webhooks are rejected. Generate or enter a shared secret below, save, and send it from the ticketing system in the X-Ticketing-Secret header.",
      };
}
