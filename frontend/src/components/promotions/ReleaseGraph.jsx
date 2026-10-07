import { useMemo } from "react";

import { dayKey, fmtDay, fmtTime } from "./timetableModel.js";

const LABELLED = ["approval", "exception", "failed", "held", "refused", "rejected"];

const W = 1200;
const H = 330;
const L = 96;
const R = 16;
const T = 36;
const B = 32;
const DAY = 86400000;

const TONE = {
  promoted: "var(--ok)",
  failed: "var(--danger)",
  refused: "var(--danger)",
  rejected: "var(--danger)",
  approval: "var(--warn)",
  exception: "var(--warn)",
  held: "var(--warn)",
  ready: "var(--tt-go)",
  promoting: "var(--tt-go)",
};

/**
 * The release graph: time left to right (three days back, two ahead),
 * environments top to bottom. Every release is a line down one step of the
 * ladder; a line that crosses an environment without stopping skipped it.
 */
export default function ReleaseGraph({ timetable, environments, selectedKey, onSelect }) {
  const tz = timetable.timezone;
  const now = new Date(timetable.now).getTime();
  const start = now - 3 * DAY;
  const end = now + 2 * DAY;
  const X = (ms) => L + ((ms - start) / (end - start)) * (W - L - R);
  const Y = (i) => T + i * ((H - T - B) / Math.max(1, environments.length - 1));
  const idx = (id) => environments.findIndex((e) => e.id === id);

  const days = useMemo(() => {
    const out = [];
    const first = new Date(start);
    for (let d = 0; d <= 6; d += 1) {
      const ms = new Date(first.getFullYear(), first.getMonth(), first.getDate() + d).getTime();
      if (ms > end) break;
      out.push(ms);
    }
    return out;
  }, [start, end]);

  const lines = timetable.departures
    .filter((d) => d.departsAt && d.kind !== "ondemand")
    .map((d) => {
      const at = new Date(d.departsAt).getTime();
      if (at < start || at > end) return null;
      const a = idx(d.fromEnvironmentId);
      const b = idx(d.toEnvironmentId);
      if (a < 0 || b < 0) return null;
      const arrive = at + (b - a) * 40 * 60000;
      const sel = d.key === selectedKey;
      const future = ["boarding", "scheduled"].includes(d.status);
      const color = sel ? "var(--tt-go)" : TONE[d.status] || "var(--text-muted)";
      const apps = d.release?.applications || 0;
      return { d, at, arrive, a, b, sel, future, color, w: sel ? 4 : 1.6 + Math.min(3, apps / 12) };
    })
    .filter(Boolean);

  // Labels for departures close together alternate above and below the
  // line instead of printing on top of each other.
  const labelY = new Map();
  const placed = [];
  lines
    .filter((l) => l.sel || LABELLED.includes(l.d.status))
    .sort((p, q) => p.at - q.at)
    .forEach((l) => {
      const x = X(l.at);
      const taken = new Set(placed.filter((o) => o.a === l.a && Math.abs(o.x - x) < 92).map((o) => o.level));
      let level = 0;
      while (taken.has(level)) level += 1;
      placed.push({ a: l.a, x, level });
      const step = Math.floor(level / 2);
      labelY.set(l.d.key, level % 2 === 0 ? Y(l.a) - 8 - step * 13 : Y(l.a) + 17 + step * 13);
    });

  return (
    <section className="tt-graph" aria-label="Release graph">
      <div className="tt-sec-head">
        <div>
          <p className="tt-eyebrow">Three days back, two ahead</p>
          <h2>Release graph</h2>
        </div>
        <p>
          Time runs left to right, environments top to bottom. Each line is one release moving down the ladder; a line that crosses an
          environment without stopping skipped it. Click a line to open its release.
        </p>
      </div>
      <div className="tt-graph-scroll">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Releases by environment and time">
          {days.map((ms) => {
            const x = Math.max(L, X(ms));
            const isToday = dayKey(new Date(ms).toISOString(), tz) === dayKey(timetable.now, tz);
            return (
              <g key={ms}>
                {isToday && <rect x={x} y={T - 24} width={Math.min(W - R, X(ms + DAY)) - x} height={H - T - B + 32} fill="var(--tt-go-soft)" />}
                <line x1={x} y1={T - 24} x2={x} y2={H - B + 8} stroke="var(--border)" />
                <text x={x + 8} y={T - 11} fontSize="13" fontWeight="700" fill="var(--text-subtle)">
                  {fmtDay(new Date(ms + 12 * 3600000).toISOString(), tz)} {new Date(ms + 12 * 3600000).getDate()}
                </text>
                {[6, 12, 18].map((h) => {
                  const xh = X(ms + h * 3600000);
                  if (xh < L || xh > W - R) return null;
                  return (
                    <g key={h}>
                      <line x1={xh} y1={T} x2={xh} y2={H - B} stroke="var(--border-soft)" />
                      <text x={xh} y={H - B + 19} fontSize="11" textAnchor="middle" fill="var(--text-muted)">
                        {String(h).padStart(2, "0")}:00
                      </text>
                    </g>
                  );
                })}
              </g>
            );
          })}
          {environments.map((env, i) => (
            <g key={env.id}>
              <line x1={L} y1={Y(i)} x2={W - R} y2={Y(i)} stroke="var(--border-strong)" />
              <text x={L - 12} y={Y(i) + 5} fontSize="14" fontWeight="750" textAnchor="end" fill="var(--text-strong)" style={{ fontFamily: "var(--font-display)" }}>
                {env.name}
              </text>
            </g>
          ))}
          {lines.map(({ d, at, arrive, a, b, sel, future, color, w }) => (
            <g key={d.key} className="tt-graph-line" onClick={() => onSelect(d.key)} role="button" aria-label={`${d.code} ${d.status}`}>
              <line x1={X(at)} y1={Y(a)} x2={X(arrive)} y2={Y(b)} stroke="transparent" strokeWidth="14" />
              <line x1={X(at)} y1={Y(a)} x2={X(arrive)} y2={Y(b)} stroke={color} strokeWidth={w} strokeLinecap="round" strokeDasharray={future && !sel ? "5 4" : undefined} />
              <circle cx={X(at)} cy={Y(a)} r={sel ? 4.5 : 3} fill={color} />
              {b - a > 1 && <circle cx={X(at + (arrive - at) / 2)} cy={Y(a + 1)} r="5" fill="var(--bg-panel)" stroke="var(--warn)" strokeWidth="2" />}
              {labelY.has(d.key) && (
                <text x={X(at) + 6} y={labelY.get(d.key)} fontSize="11" fontWeight="700" fill={color} style={{ fontFamily: "var(--font-mono)" }}>
                  {d.code}
                </text>
              )}
              <title>
                {d.code} · {fmtTime(d.departsAt, tz)} · {d.status}
              </title>
            </g>
          ))}
          <line x1={X(now)} y1={T - 24} x2={X(now)} y2={H - B + 4} stroke="var(--text-strong)" strokeWidth="2" />
          <rect x={X(now) - 24} y={H - B + 6} width="48" height="18" rx="9" fill="var(--text-strong)" />
          <text x={X(now)} y={H - B + 19} fontSize="11" fontWeight="700" textAnchor="middle" fill="var(--bg-panel)" style={{ fontFamily: "var(--font-mono)" }}>
            {fmtTime(timetable.now, tz)}
          </text>
        </svg>
      </div>
      <div className="tt-legend">
        <span>
          <i style={{ background: "var(--ok)" }} />
          Promoted
        </span>
        <span>
          <i className="tt-legend-dash" />
          Scheduled
        </span>
        <span>
          <i style={{ background: "var(--warn)" }} />
          Awaiting approval / held
        </span>
        <span>
          <i style={{ background: "var(--danger)" }} />
          Failed / rolled back
        </span>
        <span>
          <i style={{ background: "var(--tt-go)" }} />
          Selected / in progress
        </span>
      </div>
    </section>
  );
}
