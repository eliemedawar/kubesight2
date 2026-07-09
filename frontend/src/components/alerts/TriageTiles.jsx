import { formatDurationShort } from "../../lib/alertFeed.js";

function IconTriangle(props) {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

function IconBell(props) {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}>
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}

function IconCheckCircle(props) {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}>
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
      <polyline points="22 4 12 14.01 9 11.01" />
    </svg>
  );
}

/** Single-series sparkline in the tile's status color (via currentColor). */
function Sparkline({ data }) {
  if (!data || data.length < 2) {
    return null;
  }
  const w = 64;
  const h = 24;
  const max = Math.max(...data, 1);
  const pts = data.map((v, i) => [
    (i / (data.length - 1)) * w,
    h - 2.5 - (v / max) * (h - 6),
  ]);
  const line = pts
    .map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`)
    .join("");
  const area = `${line}L${w},${h}L0,${h}Z`;
  const [lx, ly] = pts[pts.length - 1];
  return (
    <span className="al-spark" aria-hidden="true">
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
        <path d={area} className="al-spark-area" />
        <path d={line} className="al-spark-line" />
        <circle cx={lx} cy={ly} r="2.4" className="al-spark-dot" />
      </svg>
    </span>
  );
}

const SEVERITY_TILES = [
  { severity: "critical", label: "Critical", Icon: IconTriangle },
  { severity: "warning", label: "Warning", Icon: IconTriangle },
  { severity: "info", label: "Info", Icon: IconBell },
];

/**
 * The severity KPI strip, upgraded from read-only counters to the feed's
 * filter. Severity tiles toggle a feed filter; the fourth tile is a doorway
 * to the History tab carrying the median time-to-resolve.
 */
export default function TriageTiles({
  counts,
  sparks,
  resolved,
  activeSeverity,
  onToggleSeverity,
  onOpenHistory,
}) {
  return (
    <div className="al-tiles">
      {SEVERITY_TILES.map(({ severity, label, Icon }) => {
        const pressed = activeSeverity === severity;
        return (
          <button
            key={severity}
            type="button"
            className={`sg-kpi al-tile al-tile--${severity}`}
            aria-pressed={pressed}
            onClick={() => onToggleSeverity(severity)}
            title={pressed ? "Clear severity filter" : `Show only ${label.toLowerCase()} alerts`}
          >
            <p className="sg-kpi-label">
              <Icon />
              {label}
            </p>
            <div className="al-tile-row">
              <span className="al-tile-value">{counts[severity] ?? 0}</span>
              {sparks?.[severity] ? <Sparkline data={sparks[severity]} /> : null}
            </div>
            <span className="al-tile-flag" aria-hidden="true">filtering</span>
          </button>
        );
      })}

      <button
        type="button"
        className="sg-kpi al-tile al-tile--resolved"
        onClick={onOpenHistory}
        title="Open alert history"
      >
        <p className="sg-kpi-label">
          <IconCheckCircle />
          Resolved · 24 h
        </p>
        <div className="al-tile-row">
          <span className="al-tile-value">{resolved ? resolved.count : "—"}</span>
          <span className="al-tile-sub">
            {resolved?.medianMs != null
              ? `median ${formatDurationShort(resolved.medianMs)} to resolve`
              : "history →"}
          </span>
          {sparks?.resolved ? <Sparkline data={sparks.resolved} /> : null}
        </div>
      </button>
    </div>
  );
}
