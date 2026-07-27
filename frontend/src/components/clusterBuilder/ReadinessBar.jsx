/** Readiness — "can I start a build right now?"
 *
 *  This replaces a strip that counted connections and profiles. Counts never
 *  answered the question the page opens with, and they hid the thing that
 *  actually breaks builds: a source that passed once and has not been
 *  exercised since.
 */

const RING_COLORS = {
  ready: "var(--ok)",
  attention: "var(--warn)",
  blocked: "var(--danger)",
};

const DOT_COLORS = {
  ok: "var(--ok)",
  live: "var(--info)",
  warn: "var(--warn)",
  bad: "var(--danger)",
  idle: "var(--text-muted)",
};

export default function ReadinessBar({ readiness, onOpenSources }) {
  const { segments, state, headline, sub, okCount, total } = readiness;
  const good = total ? (okCount / total) * 100 : 0;
  const ringStyle = {
    background: `conic-gradient(var(--ok) 0 ${good}%, ${RING_COLORS[state]} ${good}% 100%)`,
  };

  return (
    <div className="card sg-cb-ready">
      <div className="sg-cb-ready-verdict">
        <div
          className="sg-cb-ready-ring"
          style={ringStyle}
          role="img"
          aria-label={`${okCount} of ${total} sources ready`}
        >
          <i className={`is-${state}`}>{state === "ready" ? "✓" : state === "blocked" ? "✕" : "!"}</i>
        </div>
        <div>
          <b>{headline}</b>
          <span>{sub}</span>
        </div>
      </div>
      {segments.map((segment) => (
        <button
          key={segment.key}
          type="button"
          className={`sg-cb-rseg is-${segment.state}`}
          onClick={() => onOpenSources?.(segment.key)}
        >
          <span className="k">{segment.label}</span>
          <span className="v">
            <span className="sg-cb-dot" style={{ background: DOT_COLORS[segment.state] }} />
            {segment.value}
          </span>
          {segment.fix
            ? <span className="fix">{segment.fix}</span>
            : segment.sub ? <span className="s">{segment.sub}</span> : null}
        </button>
      ))}
    </div>
  );
}
