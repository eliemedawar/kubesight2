/**
 * What to tell someone about the approval emails a submit triggered.
 *
 * Approver and alert emails go out in the background now: the API answers
 * `{status: "queued", queued: N, ...}` before any is delivered (a slow SMTP
 * server must not hold the request), so "N recipients notified" would claim
 * more than is known. `sent` is only trustworthy when nothing was queued.
 *
 * Returns { kind: "queued" | "sent" | "skipped" | "failed" | "none", text }.
 */
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function describeEmailResult(result) {
  if (!result || typeof result !== "object") return { kind: "none", text: "" };
  const queued = Number(result.queued) || 0;
  const sent = Number(result.sent) || 0;
  if (result.status === "queued" || queued > 0) {
    const n = queued || sent;
    return {
      kind: "queued",
      text: n
        ? `approval emails to ${plural(n, "recipient")} are queued and go out in the background`
        : "approval emails are queued and go out in the background",
    };
  }
  if (sent > 0) return { kind: "sent", text: `${plural(sent, "recipient")} notified by email` };
  if (result.skipped) {
    return { kind: "skipped", text: `email notification was skipped (${result.reason || "no recipients configured"})` };
  }
  if (Array.isArray(result.errors) && result.errors.length) {
    return { kind: "failed", text: "the approval emails could not be sent" };
  }
  return { kind: "none", text: "" };
}
