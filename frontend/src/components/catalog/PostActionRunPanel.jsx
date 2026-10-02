import { parseApiTime } from "../../lib/apiTime.js";
import { formatRelative } from "./ciShared.jsx";
import { postRunVerdict, webhookFormatLabel, whenLabel } from "./pipeline/postActionModel.js";

const KIND_LABELS = { email: "Email", webhook: "Webhook", commands: "Cleanup commands" };

/** "in 40s" / "in 2m" for a retry that has not happened yet. */
function formatIn(iso) {
  const at = parseApiTime(iso);
  if (Number.isNaN(at)) return "soon";
  const seconds = Math.round((at - Date.now()) / 1000);
  if (seconds <= 5) return "any moment";
  if (seconds < 60) return `in ${seconds}s`;
  return `in ${Math.ceil(seconds / 60)}m`;
}

/** The word a post-action row shows where a stage shows its duration. */
export function postRunState(item) {
  const notify = item.type === "email" || item.type === "webhook";
  if (item.status === "success") return notify ? "sent" : "ran";
  if (item.status === "skipped") return notify ? "not sent" : "skipped";
  if (item.status === "pending") return notify && item.phase === "queued" ? (item.attempts ? "retrying" : "queued") : "waiting";
  if (item.status === "running") return notify ? "sending" : "running";
  if (item.status === "timeout") return "timed out";
  if (item.status === "cancelled") return "cancelled";
  return "failed";
}

/**
 * What one post action did on this build, above its log: the kind, when it
 * fires, what it targeted (recipients, the webhook's secret NAME — never its
 * URL), and the verdict in words. A failed cleanup says plainly that it did
 * not change the build's result.
 */
export default function PostActionRunPanel({ item }) {
  const notify = item.type === "email" || item.type === "webhook";
  const failed = ["failed", "timeout"].includes(item.status);
  return (
    <section className={`sg-ci-post-panel is-${item.status}`} aria-label="Post action">
      <header>
        <span className="sg-ci-post-kind">{KIND_LABELS[item.type] || "Post action"}</span>
        <span className="sg-ci-post-when">{whenLabel(item.when)}</span>
        {item.trigger && notify && <span className="muted">· build {item.trigger}</span>}
      </header>
      <p className="sg-ci-post-verdict">{postRunVerdict(item)}</p>
      <dl className="sg-ci-post-facts">
        {item.type === "email" && item.recipients?.length > 0 && (
          <>
            <dt>To</dt>
            <dd>{item.recipients.join(", ")}</dd>
          </>
        )}
        {item.type === "webhook" && (
          <>
            <dt>Format</dt>
            <dd>{webhookFormatLabel(item.format)}</dd>
            <dt>URL from</dt>
            <dd>
              <code>{item.urlSecret || "—"}</code> <span className="muted">(secret)</span>
            </dd>
          </>
        )}
        {item.type === "commands" && (
          <>
            <dt>Ran with</dt>
            <dd>
              {item.stagesResult ? `stages ${item.stagesResult === "success" ? "succeeded" : "failed"}` : "—"}
              {item.image ? (
                <>
                  {" "}
                  · <code>{item.image}</code>
                </>
              ) : null}
            </dd>
          </>
        )}
        {notify && item.attempts > 0 && (
          <>
            <dt>Attempts</dt>
            <dd>
              {item.attempts}
              {item.nextAttemptAt && <> · next try {formatIn(item.nextAttemptAt)}</>}
            </dd>
          </>
        )}
        {item.deliveredAt && (
          <>
            <dt>Delivered</dt>
            <dd>{formatRelative(item.deliveredAt)}</dd>
          </>
        )}
      </dl>
      {failed && item.type === "commands" && (
        <p className="field-hint">A cleanup never changes the build's result — the build keeps its own status.</p>
      )}
    </section>
  );
}
