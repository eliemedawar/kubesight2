// Canvas drawing primitives ported from the KubeSight Operations Dashboard
// reference mock. These are pure draw functions: the caller (useCanvasChart)
// is responsible for devicePixelRatio scaling and clearing the surface, so
// every function here works in CSS pixels against an already-scaled context.

// Axis / grid colors lifted verbatim from the reference so the visual style
// (mono axis labels, faint horizontal gridlines) matches pixel-for-pixel.
const AXIS_LABEL = "#4d5566";
const GRID_LINE = "#1a1e29";
const LIMIT_COLOR = "#f5b945";

// Resolve a CSS custom property (e.g. "--accent") to a concrete color string so
// it can be handed to the canvas API, which cannot consume CSS variables. The
// accent is theme-driven, so charts stay in sync with the active accent color.
export function cssVar(name, fallback) {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

// Convert a #rgb / #rrggbb hex (or pass-through rgb/rgba) to an rgba() string.
export function hexA(hex, a) {
  if (typeof hex === "string" && hex.startsWith("#")) {
    let h = hex.slice(1);
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }
  return hex;
}

// Horizontal gridlines + right-aligned monospace axis labels.
// `mono` is an optional unit suffix appended to each label (e.g. "%").
export function grid(ctx, w, h, padL, padB, maxY, steps, mono) {
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillStyle = AXIS_LABEL;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= steps; i++) {
    const val = maxY - (maxY / steps) * i;
    const y = (h - padB) * (i / steps);
    ctx.strokeStyle = GRID_LINE;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w, y);
    ctx.stroke();
    ctx.fillText(Math.round(val) + (mono || ""), padL - 6, y + (i === 0 ? 6 : 0));
  }
}

// Stacked area chart (e.g. CPU by namespace). `arrs` is an array of equal-length
// series; each is drawn cumulatively on top of the previous with a vertical
// gradient fill and a thin stroke along its top edge.
export function drawStacked(ctx, w, h, arrs, colors, maxY, mono) {
  const padL = 30;
  const padB = 4;
  grid(ctx, w, h, padL, padB, maxY, 4, mono);
  const n = arrs[0].length;
  const cw = w - padL;
  const xy = (i, v) => [padL + (cw * i) / (n - 1), h - padB - (v / maxY) * (h - padB)];
  const cum = new Array(n).fill(0);
  for (let s = 0; s < arrs.length; s++) {
    const top = cum.map((c, i) => c + arrs[s][i]);
    ctx.beginPath();
    ctx.moveTo(...xy(0, cum[0]));
    for (let i = 0; i < n; i++) ctx.lineTo(...xy(i, top[i]));
    for (let i = n - 1; i >= 0; i--) ctx.lineTo(...xy(i, cum[i]));
    ctx.closePath();
    const grd = ctx.createLinearGradient(0, 0, 0, h);
    grd.addColorStop(0, hexA(colors[s], 0.55));
    grd.addColorStop(1, hexA(colors[s], 0.12));
    ctx.fillStyle = grd;
    ctx.fill();
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const pt = xy(i, top[i]);
      i ? ctx.lineTo(...pt) : ctx.moveTo(...pt);
    }
    ctx.strokeStyle = colors[s];
    ctx.lineWidth = 1.6;
    ctx.stroke();
    for (let i = 0; i < n; i++) cum[i] = top[i];
  }
}

// Single area chart with gradient fill, a 2px stroke, and an optional dashed
// amber limit line (e.g. memory pressure threshold).
export function drawArea(ctx, w, h, arr, color, maxY, limit, mono) {
  const padL = 30;
  const padB = 4;
  grid(ctx, w, h, padL, padB, maxY, 4, mono);
  const n = arr.length;
  const cw = w - padL;
  const xy = (i, v) => [padL + (cw * i) / (n - 1), h - padB - (v / maxY) * (h - padB)];
  ctx.beginPath();
  ctx.moveTo(padL, h - padB);
  for (let i = 0; i < n; i++) ctx.lineTo(...xy(i, arr[i]));
  ctx.lineTo(w, h - padB);
  ctx.closePath();
  const grd = ctx.createLinearGradient(0, 0, 0, h);
  grd.addColorStop(0, hexA(color, 0.5));
  grd.addColorStop(1, hexA(color, 0.05));
  ctx.fillStyle = grd;
  ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const pt = xy(i, arr[i]);
    i ? ctx.lineTo(...pt) : ctx.moveTo(...pt);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();
  if (limit) {
    const ly = h - padB - (limit / maxY) * (h - padB);
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = LIMIT_COLOR;
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(padL, ly);
    ctx.lineTo(w, ly);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

// Multiple overlaid line series (e.g. network ingress/egress) with a faint
// gradient wash under each line.
export function drawLines(ctx, w, h, arrs, colors, maxY, mono) {
  const padL = 38;
  const padB = 4;
  grid(ctx, w, h, padL, padB, maxY, 4, mono);
  const n = arrs[0].length;
  const cw = w - padL;
  const xy = (i, v) => [padL + (cw * i) / (n - 1), h - padB - (v / maxY) * (h - padB)];
  arrs.forEach((arr, s) => {
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const pt = xy(i, arr[i]);
      i ? ctx.lineTo(...pt) : ctx.moveTo(...pt);
    }
    ctx.strokeStyle = colors[s];
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();
    const grd = ctx.createLinearGradient(0, 0, 0, h);
    grd.addColorStop(0, hexA(colors[s], 0.22));
    grd.addColorStop(1, hexA(colors[s], 0));
    ctx.lineTo(w, h - padB);
    ctx.lineTo(padL, h - padB);
    ctx.closePath();
    ctx.fillStyle = grd;
    ctx.fill();
  });
}

// Compact sparkline for KPI cards: last ~22 points, auto-scaled to its own
// min/max, gradient fill fading to transparent.
export function spark(ctx, w, h, arr, color) {
  const slice = arr.slice(-22);
  const mn = Math.min(...slice);
  const mx = Math.max(...slice);
  const rg = mx - mn || 1;
  const xy = (i, v) => [(w * i) / (slice.length - 1), h - 3 - ((v - mn) / rg) * (h - 6)];
  ctx.beginPath();
  ctx.moveTo(0, h);
  for (let i = 0; i < slice.length; i++) ctx.lineTo(...xy(i, slice[i]));
  ctx.lineTo(w, h);
  ctx.closePath();
  const grd = ctx.createLinearGradient(0, 0, 0, h);
  grd.addColorStop(0, hexA(color, 0.4));
  grd.addColorStop(1, hexA(color, 0));
  ctx.fillStyle = grd;
  ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < slice.length; i++) {
    const pt = xy(i, slice[i]);
    i ? ctx.lineTo(...pt) : ctx.moveTo(...pt);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();
}
