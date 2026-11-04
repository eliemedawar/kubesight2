import { useState } from "react";
import { downloadMergeCheckReport } from "../../../api/mergeChecksApi.js";
import { formatRelative, shortSha } from "../ciShared.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import { runBreakdown } from "./mergeCheckModel.js";

const VERDICT = {
  allowed: { label: "Merge allowed", tone: "ok", icon: "check" },
  blocked: { label: "Merge blocked", tone: "error", icon: "x" },
  unknown: { label: "Not checked", tone: "warn", icon: "alert" },
};

// A report exists once a check has an answer; the server says the same.
const REPORTABLE = ["passed", "failed", "error"];

/**
 * One readable PDF per pull request: every check's findings in one document,
 * for the people who act on a blocked merge without reading build logs.
 *
 * Built on the server when downloaded — not one of the build's artifacts, and
 * never out of step with the verdict it describes.
 */
export default function ReportsView({ runs, loading }) {
  const [busyId, setBusyId] = useState(null);
  const [failure, setFailure] = useState({ id: null, message: "" });
  const ready = runs.filter((run) => REPORTABLE.includes(run.state));
  const pending = runs.length - ready.length;

  const download = async (run) => {
    setBusyId(run.id);
    setFailure({ id: null, message: "" });
    try {
      await downloadMergeCheckReport(run.id);
    } catch (err) {
      setFailure({ id: run.id, message: err.message || "The report could not be made." });
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="mc-view">
      <header className="pl-view-head">
        <div>
          <span className="pl-kicker">One PDF per pull request</span>
          <h3>Reports</h3>
          <p>
            Every check's findings in one readable document: the verdict and why, each check against its
            limit, then the findings worst first. Made when you download it, apart from the build's artifacts.
          </p>
        </div>
      </header>

      {ready.length === 0 ? (
        <div className="pl-empty">
          <span className="pl-empty-glyph" aria-hidden="true">
            <PlIcon name="file" />
          </span>
          <strong>{loading ? "Loading…" : "No reports yet"}</strong>
          <p>
            {pending > 0
              ? "A check is still running — its report appears here when it finishes."
              : "Each pull request gets a report here once its checks have finished."}
          </p>
        </div>
      ) : (
        <>
          {pending > 0 && (
            <p className="mc-reports-note">
              {pending} {pending === 1 ? "check is" : "checks are"} still running — {pending === 1 ? "its" : "their"}{" "}
              report appears when {pending === 1 ? "it finishes" : "they finish"}.
            </p>
          )}
          <ol className="mc-runs">
            {ready.map((run) => {
              const verdict = VERDICT[run.verdict] || VERDICT.unknown;
              const ran = runBreakdown(run).filter((item) => item.status === "ok").length;
              return (
                <li key={run.id} className={`mc-run is-${verdict.tone}`}>
                  <span className="mc-run-mark" aria-hidden="true">
                    <PlIcon name={verdict.icon} />
                  </span>
                  <div className="mc-run-main">
                    <div className="mc-run-title">
                      <strong>{run.title || `Pull request #${run.pullRequestId}`}</strong>
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
                      <span>{formatRelative(run.evaluatedAt || run.createdAt)}</span>
                    </p>
                    <p className="mc-reports-sum">
                      {run.verdict === "unknown"
                        ? "No verdict — the report says why the checks could not finish."
                        : ran === 0
                          ? "No check reported a result — the report says which ones."
                          : `${run.totalProblems ?? 0} ${run.totalProblems === 1 ? "problem" : "problems"} across ${ran} ${
                            ran === 1 ? "check" : "checks"
                          }`}
                    </p>
                    {failure.id === run.id && <p className="pl-field-error">{failure.message}</p>}
                  </div>
                  <div className="mc-run-side">
                    <div className="mc-run-actions">
                      <button
                        type="button"
                        className="btn-outline btn-compact"
                        disabled={busyId === run.id}
                        onClick={() => download(run)}
                      >
                        <PlIcon name="download" />
                        {busyId === run.id ? "Preparing…" : "Download PDF"}
                      </button>
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </div>
  );
}
