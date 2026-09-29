import { formatRelative, shortSha } from "../ciShared.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import { runBreakdown, toolMonogram } from "./mergeCheckModel.js";

const VERDICT = {
  allowed: { label: "Merge allowed", tone: "ok", icon: "check" },
  blocked: { label: "Merge blocked", tone: "error", icon: "x" },
  unknown: { label: "Not checked", tone: "warn", icon: "alert" },
};

const STATE_LABEL = {
  queued: { label: "Waiting to run", tone: "info", icon: "clock" },
  running: { label: "Checking…", tone: "info", icon: "clock" },
};

const DELIVERY = {
  delivered: { label: "Reported to Bitbucket", tone: "ok" },
  pending: { label: "Reporting…", tone: "info" },
  failed: { label: "Not reported", tone: "error" },
  not_applicable: { label: "Nothing to report", tone: "muted" },
};

const METRIC_TONE = { ok: "ok", skipped: "muted", error: "error", missing: "warn" };

/**
 * What the gate decided, pull request by pull request.
 *
 * Each row answers the three questions someone opens this list with: did it
 * pass, which check said no, and does Bitbucket know. The per-check line is
 * the reason a verdict is readable without opening the build.
 */
export default function HistoryView({ runs, canEdit, busyCheckId, onRedeliver, onOpenBuild, loading }) {
  return (
    <div className="mc-view">
      <header className="pl-view-head">
        <div>
          <span className="pl-kicker">Last 25 pull requests</span>
          <h3>History</h3>
          <p>Refreshes on its own while a check is running.</p>
        </div>
      </header>

      {runs.length === 0 ? (
        <div className="pl-empty">
          <span className="pl-empty-glyph" aria-hidden="true">
            <PlIcon name="pullRequest" />
          </span>
          <strong>{loading ? "Loading…" : "No pull requests checked yet"}</strong>
          <p>
            Once the webhook is set up and checks are on, the next pull request into a watched branch
            appears here — with each check's findings and whether Bitbucket was told.
          </p>
        </div>
      ) : (
        <ol className="mc-runs">
          {runs.map((run) => {
            const verdict = VERDICT[run.verdict] || STATE_LABEL[run.state] || {
              label: run.state || "Unknown",
              tone: "muted",
              icon: "alert",
            };
            const delivery = DELIVERY[run.deliveryState] || { label: run.deliveryState || "—", tone: "muted" };
            const breakdown = runBreakdown(run);
            const cap = run.gate?.maxTotalProblems;
            return (
              <li key={run.id} className={`mc-run is-${verdict.tone}`}>
                <span className="mc-run-mark" aria-hidden="true">
                  <PlIcon name={verdict.icon} />
                </span>
                <div className="mc-run-main">
                  <div className="mc-run-title">
                    {run.pullRequestUrl ? (
                      <a href={run.pullRequestUrl} target="_blank" rel="noreferrer">
                        {run.title || `Pull request #${run.pullRequestId}`}
                      </a>
                    ) : (
                      <strong>{run.title || `Pull request #${run.pullRequestId}`}</strong>
                    )}
                    <span className={`mc-verdict is-${verdict.tone}`}>{verdict.label}</span>
                  </div>
                  <p className="mc-run-meta">
                    <PlIcon name="pullRequest" />
                    <span>#{run.pullRequestId}</span>
                    {run.author && <span>{run.author}</span>}
                    <span>
                      <code>{run.sourceBranch || "?"}</code> → <code>{run.destinationBranch || "?"}</code>
                    </span>
                    <span className="is-mono">{shortSha(run.commitSha)}</span>
                    <span>{formatRelative(run.createdAt)}</span>
                  </p>
                  {breakdown.length > 0 && (
                    <div className="mc-run-tools">
                      {breakdown.map((item) => (
                        <span
                          key={item.tool}
                          className={`mc-metric is-${METRIC_TONE[item.status] || "muted"}`}
                          title={item.message || `${item.label}: ${item.status}`}
                        >
                          <b>{toolMonogram(item.tool)}</b>
                          {item.label}
                          <em>
                            {item.status === "ok"
                              ? item.problems
                              : item.status === "skipped"
                                ? "skipped"
                                : item.status === "error"
                                  ? "failed to run"
                                  : "not run"}
                          </em>
                        </span>
                      ))}
                      {run.totalProblems !== null && run.totalProblems !== undefined && (
                        <span className="mc-run-total">
                          {run.totalProblems} total
                          {cap !== null && cap !== undefined && <> · limit {cap}</>}
                        </span>
                      )}
                    </div>
                  )}
                  {(run.reasons || []).length > 0 && (
                    <ul className="mc-run-reasons">
                      {run.reasons.slice(0, 3).map((reason, index) => (
                        <li key={index}>{reason}</li>
                      ))}
                    </ul>
                  )}
                  {run.error && <p className="pl-field-error">{run.error}</p>}
                </div>
                <div className="mc-run-side">
                  <span className={`mc-delivery is-${delivery.tone}`} title={run.deliveryError || ""}>
                    {delivery.label}
                  </span>
                  {run.deliveryError && <small className="mc-run-error">{run.deliveryError}</small>}
                  <div className="mc-run-actions">
                    {run.buildId && (
                      <button type="button" className="btn-ghost mc-run-link" onClick={() => onOpenBuild(run.buildId)}>
                        <PlIcon name="terminal" /> Build {run.buildNumber ? `#${run.buildNumber}` : ""}
                      </button>
                    )}
                    {canEdit && run.verdict && run.deliveryState !== "delivered" && (
                      <button
                        type="button"
                        className="btn-outline btn-compact"
                        disabled={busyCheckId === run.id}
                        onClick={() => onRedeliver(run.id)}
                      >
                        <PlIcon name="refresh" />
                        {busyCheckId === run.id ? "Sending…" : "Send again"}
                      </button>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
