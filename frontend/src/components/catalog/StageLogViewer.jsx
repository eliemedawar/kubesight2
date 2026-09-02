import { useCallback, useEffect, useRef, useState } from "react";
import { getCiStageLogs } from "../../api/ciApi.js";
import { getBaseUrl } from "../../api/client.js";
import { ciStageLogDownloadPath } from "../../api/ciApi.js";

const POLL_MS = 2000;

/**
 * Append-only log view for one build stage.
 *
 * Reads by offset (`after=<seq>`) rather than holding a stream open: the
 * backend runs a single gunicorn worker with a small thread pool, and a
 * long-lived SSE connection per viewer would pin one of those threads.
 * Polling stops as soon as the stage is terminal and fully drained.
 */
export default function StageLogViewer({ buildId, stage }) {
  const [lines, setLines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [follow, setFollow] = useState(true);

  const cursorRef = useRef(0);
  const bodyRef = useRef(null);
  const timerRef = useRef(null);
  const followRef = useRef(follow);
  followRef.current = follow;

  // Reset when the selected stage changes — a stale cursor would silently skip
  // the new stage's first lines.
  useEffect(() => {
    cursorRef.current = 0;
    setLines([]);
    setLoading(true);
    setError("");
  }, [stage?.id]);

  const poll = useCallback(async () => {
    if (!buildId || !stage?.id) return true;
    try {
      const data = await getCiStageLogs(buildId, stage.id, cursorRef.current, 2000);
      if (data.lines?.length) {
        cursorRef.current = Math.max(cursorRef.current, data.nextSeq);
        // De-dupe by seq: overlapping polls (StrictMode double-mount, a reset
        // racing an in-flight request) must never render a line twice.
        setLines((prev) => {
          const seen = new Set(prev.map((line) => line.seq));
          const fresh = data.lines.filter((line) => !seen.has(line.seq));
          return fresh.length ? [...prev, ...fresh] : prev;
        });
      }
      setError("");
      return Boolean(data.complete);
    } catch (err) {
      setError(err.message || "Could not load logs.");
      return false;
    } finally {
      setLoading(false);
    }
  }, [buildId, stage?.id]);

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      const done = await poll();
      if (cancelled || done) return;
      timerRef.current = window.setTimeout(tick, POLL_MS);
    };
    tick();

    return () => {
      cancelled = true;
      window.clearTimeout(timerRef.current);
    };
  }, [poll]);

  useEffect(() => {
    if (followRef.current && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [lines]);

  const onScroll = (event) => {
    const element = event.currentTarget;
    const atBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 24;
    if (atBottom !== followRef.current) setFollow(atBottom);
  };

  if (!stage) return null;

  // The single most repeated manual action in any CI log: find the failure.
  // stderr lines and ERROR-ish content qualify; the first one wins.
  const firstError = lines.find(
    (line) =>
      line.stream === "stderr" ||
      /\b(error|failed|failure|exception|traceback)\b/i.test(line.content || "")
  );
  const firstErrorSeq = firstError ? firstError.seq : null;

  const jumpToFirstError = () => {
    setFollow(false);
    const target = bodyRef.current?.querySelector(`[data-seq="${firstErrorSeq}"]`);
    if (target) target.scrollIntoView({ block: "center" });
  };

  const downloadUrl = `${getBaseUrl()}${ciStageLogDownloadPath(buildId, stage.id)}`;

  return (
    <div className="sg-ci-logs">
      <div className="sg-ci-logs-bar">
        <span className="sg-ci-logs-title">
          {stage.name}
          {stage.exitCode != null && (
            <span className="muted"> · exit {stage.exitCode}</span>
          )}
        </span>
        {firstErrorSeq !== null && (
          <button
            type="button"
            className="btn-outline btn-compact sg-ci-jump-error"
            onClick={jumpToFirstError}
          >
            Jump to first error
          </button>
        )}
        <label className="sg-ci-logs-follow">
          <input
            type="checkbox"
            checked={follow}
            onChange={(event) => setFollow(event.target.checked)}
          />
          Follow
        </label>
        {lines.length > 0 && (
          <a className="btn-outline btn-compact" href={downloadUrl} download>
            Download
          </a>
        )}
      </div>

      {stage.error && <p className="banner-message error">{stage.error}</p>}
      {error && <p className="banner-message error">{error}</p>}

      <pre className="sg-ci-logs-body" ref={bodyRef} onScroll={onScroll} tabIndex={0}>
        {loading && !lines.length
          ? "Loading…"
          : lines.length
          ? lines.map((line) => (
              <span
                key={line.seq}
                data-seq={line.seq}
                className={`sg-ci-log-line sg-ci-log-line--${line.stream}`}
              >
                {line.content || " "}
                {"\n"}
              </span>
            ))
          : stage.status === "pending"
          ? "This stage has not started yet."
          : stage.status === "skipped"
          ? "This stage was skipped because an earlier stage failed."
          : "No output."}
      </pre>

      {stage.logTruncated && (
        <p className="field-hint">
          Output was truncated — this stage exceeded the per-stage log limit.
        </p>
      )}
    </div>
  );
}
