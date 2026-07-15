import { useEffect, useMemo, useState } from "react";

// Shared, read-only topology renderer used by the Application Services detail
// view and the Client Service Access Topology overlay. The layout helpers are
// exported so the interactive Topology editor can reuse the same geometry.

// Component health → topology node indicator color (kept as a public export).
export const TOPO_STATUS_COLOR = {
  healthy: "var(--ok)",
  degraded: "var(--warn)",
  unhealthy: "var(--danger)",
  unknown: "var(--text-muted)",
};

// Component health → Signal status tone (drives the node status bar, the icon
// chip tint and — via the worst endpoint — the edge stroke).
const STATUS_TONE = {
  healthy: "ok",
  degraded: "warn",
  unhealthy: "danger",
  unknown: "muted",
};

// Status bar fills (3px inset bar on the card's left, per the Signal topo skin).
const BAR_FILL = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  danger: "var(--danger)",
  muted: "var(--border-strong)",
};

// Icon chip tints (mirrors .sg-ico--* in styles/signal/screens.css).
const CHIP_COLORS = {
  ok: { bg: "var(--ok-soft)", fg: "var(--ok)" },
  warn: { bg: "var(--warn-soft)", fg: "var(--warn)" },
  danger: { bg: "var(--danger-soft)", fg: "var(--danger)" },
  accent: { bg: "var(--accent-soft)", fg: "var(--accent-strong)" },
  muted: { bg: "var(--bg-interactive)", fg: "var(--text-subtle)" },
};

export const NODE_W = 160;
export const NODE_H = 48;
export const PAD = 40;

// Point where the ray from (cx,cy) toward (nx,ny) exits a rectangle of (w,h)
export function rectExitPoint(cx, cy, nx, ny, w = NODE_W, h = NODE_H) {
  const tx = Math.abs(nx) < 1e-9 ? Infinity : (w / 2) / Math.abs(nx);
  const ty = Math.abs(ny) < 1e-9 ? Infinity : (h / 2) / Math.abs(ny);
  const t = Math.min(tx, ty);
  return { x: cx + nx * t, y: cy + ny * t };
}

export function nodeExitPoint(cx, cy, nx, ny) {
  return rectExitPoint(cx, cy, nx, ny, NODE_W, NODE_H);
}

// A node's manually-saved position, if it has one.
function savedNodePos(n) {
  const x = typeof n.x === "number" ? n.x : n.positionX;
  const y = typeof n.y === "number" ? n.y : n.positionY;
  return typeof x === "number" && typeof y === "number" ? { x, y } : null;
}

export function computeLayout(nodes, edges) {
  const n = nodes.length;
  if (!n) return { positions: {}, svgW: NODE_W + PAD * 2, svgH: NODE_H + PAD * 2 };

  // If every node has a saved position, honor the user's manual layout.
  if (nodes.every((nd) => savedNodePos(nd))) {
    const raw = {};
    nodes.forEach((nd) => { raw[String(nd.id)] = savedNodePos(nd); });
    const xs = nodes.map((nd) => raw[String(nd.id)].x);
    const ys = nodes.map((nd) => raw[String(nd.id)].y);
    const minX = Math.min(...xs), minY = Math.min(...ys);
    const maxX = Math.max(...xs), maxY = Math.max(...ys);
    const positions = {};
    nodes.forEach((nd) => {
      positions[String(nd.id)] = { x: raw[String(nd.id)].x - minX, y: raw[String(nd.id)].y - minY };
    });
    return {
      positions,
      svgW: (maxX - minX) + NODE_W + PAD * 2,
      svgH: (maxY - minY) + NODE_H + PAD * 2,
    };
  }
  if (n === 1) {
    return {
      positions: { [String(nodes[0].id)]: { x: 0, y: 0 } },
      svgW: NODE_W + PAD * 2,
      svgH: NODE_H + PAD * 2,
    };
  }

  // Layered (Sugiyama-style) top-to-bottom layout. Topologies are directed
  // flows (Client → Transport → entrypoint → downstream), so ranking nodes by
  // their longest path from a root and stacking layers vertically produces a
  // readable hierarchy instead of the scattered blob a force layout gives.
  const ids = nodes.map((nd) => String(nd.id));
  const idSet = new Set(ids);

  const outAdj = {}, inAdj = {};
  ids.forEach((id) => { outAdj[id] = []; inAdj[id] = []; });
  const seenPair = new Set();
  edges.forEach((e) => {
    const s = String(e.sourceNodeId), t = String(e.targetNodeId);
    if (s === t || !idSet.has(s) || !idSet.has(t)) return;
    const key = `${s}→${t}`;
    if (seenPair.has(key)) return;
    seenPair.add(key);
    // The reverse half of a bidirectional pair (A ⇄ B) would read as a
    // 2-cycle here and inflate both ranks to the iteration cap; drop it from
    // the layout graph (the drawn edges are unaffected).
    if (seenPair.has(`${t}→${s}`)) return;
    outAdj[s].push(t);
    inAdj[t].push(s);
  });

  // Longest-path ranking via relaxation (iteration count caps any cycle).
  const rank = {};
  ids.forEach((id) => { rank[id] = 0; });
  for (let iter = 0; iter < ids.length; iter++) {
    let changed = false;
    ids.forEach((s) => {
      outAdj[s].forEach((t) => {
        if (rank[t] < rank[s] + 1) { rank[t] = rank[s] + 1; changed = true; }
      });
    });
    if (!changed) break;
  }

  // Bucket nodes into layers by rank (input order preserved within each
  // layer). Ranks are first compacted to consecutive indices: a cycle longer
  // than two nodes still inflates ranks unevenly, and the gaps would leave
  // holes in `layers` (empty bands in the drawing, NaN from the spread below).
  const usedRanks = [...new Set(ids.map((id) => rank[id]))].sort((a, b) => a - b);
  const rankIndex = new Map(usedRanks.map((r, i) => [r, i]));
  ids.forEach((id) => { rank[id] = rankIndex.get(rank[id]); });

  const layers = [];
  ids.forEach((id) => {
    const r = rank[id];
    (layers[r] = layers[r] || []).push(id);
  });

  // Reduce edge crossings: order each layer by the mean position of its parents
  // in the layer above (barycenter heuristic, a few passes).
  for (let pass = 0; pass < 4; pass++) {
    for (let r = 1; r < layers.length; r++) {
      if (!layers[r] || !layers[r - 1]) continue;
      const prevPos = {};
      layers[r - 1].forEach((id, i) => { prevPos[id] = i; });
      layers[r] = layers[r]
        .map((id, i) => {
          const parents = inAdj[id].filter((p) => rank[p] === r - 1);
          const bc = parents.length
            ? parents.reduce((a, p) => a + prevPos[p], 0) / parents.length
            : i;
          return { id, bc };
        })
        .sort((a, b) => a.bc - b.bc)
        .map((o) => o.id);
    }
  }

  const H_GAP = NODE_W + 70;   // horizontal spacing within a layer
  const V_GAP = NODE_H + 90;   // vertical spacing between layers
  const maxCount = Math.max(1, ...layers.map((l) => (l ? l.length : 0)));
  const rowWidth = (maxCount - 1) * H_GAP;

  const positions = {};
  layers.forEach((layer, r) => {
    if (!layer) return;
    const layerW = (layer.length - 1) * H_GAP;
    const offsetX = (rowWidth - layerW) / 2; // center each layer
    layer.forEach((id, i) => {
      positions[id] = { x: offsetX + i * H_GAP, y: r * V_GAP };
    });
  });

  const layerCount = layers.length;
  return {
    positions,
    svgW: rowWidth + NODE_W + PAD * 2,
    svgH: (layerCount - 1) * V_GAP + NODE_H + PAD * 2,
  };
}

// Icon chip glyph per node kind: users for clients, shield for transports,
// cube for service components. Inline SVG strokes only (no emoji).
function NodeGlyph({ kind, cx, cy, color }) {
  const common = {
    fill: "none",
    stroke: color,
    strokeWidth: 1.4,
    strokeLinecap: "round",
    strokeLinejoin: "round",
  };
  if (kind === "client") {
    return (
      <g transform={`translate(${cx},${cy})`} aria-hidden="true">
        <circle cx={0} cy={-2.1} r={2.2} {...common} />
        <path d="M -4 4.4 C -4 0.9 4 0.9 4 4.4" {...common} />
      </g>
    );
  }
  if (kind === "transport") {
    return (
      <g transform={`translate(${cx},${cy})`} aria-hidden="true">
        <path d="M 0 -4.8 L 4 -3.2 V 0.4 C 4 2.6 2.2 4.2 0 4.9 C -2.2 4.2 -4 2.6 -4 0.4 V -3.2 Z" {...common} />
      </g>
    );
  }
  return (
    <g transform={`translate(${cx},${cy})`} aria-hidden="true">
      <path d="M -4.2 -2.4 L 0 -4.6 L 4.2 -2.4 V 2.4 L 0 4.6 L -4.2 2.4 Z" {...common} />
      <path d="M -4.2 -2.4 L 0 -0.4 L 4.2 -2.4 M 0 -0.4 V 4.6" {...common} />
    </g>
  );
}

// Chip tint: overlay kind wins for client/transport (identity chips per the
// Signal concept); everything else is tinted by its real component health.
function chipToneFor(node) {
  if (node.overlay === "client") return "muted";
  if (node.overlay === "transport") return "accent";
  return STATUS_TONE[node.componentStatus] || "muted";
}

function truncate(str, max) {
  if (!str) return "";
  return str.length > max ? str.slice(0, max - 1) + "…" : str;
}

export default function TopologyViewer({ nodes, edges, compact = false, fillWidth = false, allowFullscreen = true }) {
  const { positions, svgW, svgH } = useMemo(
    () => computeLayout(nodes || [], edges || []),
    [nodes, edges]
  );

  const nodeById = useMemo(() => {
    const map = {};
    (nodes || []).forEach((n) => { map[String(n.id)] = n; });
    return map;
  }, [nodes]);

  const [fullscreen, setFullscreen] = useState(false);

  // Exit fullscreen on Escape. Capture phase + stopPropagation so the parent
  // modal's own Escape-to-close handler (service/client detail) doesn't also
  // fire — one Escape closes only the fullscreen layer.
  useEffect(() => {
    if (!fullscreen) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setFullscreen(false);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [fullscreen]);

  if (!nodes || nodes.length === 0) {
    return <p className="muted" style={{ fontSize: "0.875rem", fontStyle: "italic" }}>No topology defined yet.</p>;
  }

  // Edge tone from real data: worst endpoint component health first (unhealthy
  // → danger, degraded → warn), then the existing scope encoding (external →
  // warn dashed, as before), nominal green only when both endpoints report
  // healthy, muted otherwise (no health data).
  const edgeToneFor = (edge) => {
    const s = nodeById[String(edge.sourceNodeId)]?.componentStatus;
    const t = nodeById[String(edge.targetNodeId)]?.componentStatus;
    if (s === "unhealthy" || t === "unhealthy") return "danger";
    if (s === "degraded" || t === "degraded") return "warn";
    if (edge.scope === "external") return "warn";
    if (s === "healthy" && t === "healthy") return "ok";
    return "muted";
  };

  // Zoom out a touch by padding the viewBox with extra margin around the
  // content, so the topology renders smaller and centered in its container.
  // fillWidth trims the margin and lets the graph scale up to fill the
  // container (used where the viewer owns a large panel, e.g. the client
  // access topology) so small graphs stay readable instead of shrinking.
  const marginFactor = fillWidth ? 0.05 : 0.12;
  const zoomMx = svgW * marginFactor;
  const zoomMy = svgH * marginFactor;
  // Never scale the SVG *up* past its natural size — otherwise a 1–2 node graph
  // gets stretched to the full container width and the nodes balloon. Cap the
  // rendered width to the viewBox width (treated as px) and center it; larger
  // graphs still scale down to fit. When fillWidth is set the cap is lifted so
  // the graph uses the full available width.
  const naturalWidth = Math.round(svgW + zoomMx * 2);

  // In fullscreen the SVG fills the viewport (aspect preserved by the viewBox);
  // inline the sizing switch so the same mounted SVG works in both modes.
  const svgStyle = fullscreen
    ? { display: "block", width: "100%", height: "100%" }
    : { display: "block", width: "100%", maxWidth: fillWidth ? "100%" : `${naturalWidth}px`, height: "auto", margin: "0 auto" };

  return (
    <div
      className={`topo-viewer${compact ? " topo-viewer--compact" : ""}${fullscreen ? " topo-viewer--fs" : ""}`}
      role={fullscreen ? "dialog" : undefined}
      aria-modal={fullscreen ? "true" : undefined}
      aria-label={fullscreen ? "Topology fullscreen view" : undefined}
    >
      {allowFullscreen ? (
        <button
          type="button"
          className="topo-fs-btn"
          onClick={() => setFullscreen((v) => !v)}
          aria-label={fullscreen ? "Exit fullscreen" : "View topology fullscreen"}
          title={fullscreen ? "Exit fullscreen (Esc)" : "Fullscreen"}
        >
          {fullscreen ? (
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3" />
            </svg>
          )}
        </button>
      ) : null}
      <svg viewBox={`${-zoomMx} ${-zoomMy} ${svgW + zoomMx * 2} ${svgH + zoomMy * 2}`}
        preserveAspectRatio="xMidYMid meet"
        style={svgStyle}>
        <defs>
          <filter id="topo-shadow" x="-30%" y="-30%" width="160%" height="160%">
            <feDropShadow dx="0" dy="2" stdDeviation="4" floodColor="rgba(0,0,0,0.5)" />
          </filter>
          <marker id="topo-arrow-ok" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="var(--ok)" />
          </marker>
          <marker id="topo-arrow-warn" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="var(--warn)" />
          </marker>
          <marker id="topo-arrow-danger" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="var(--danger)" />
          </marker>
          <marker id="topo-arrow-muted" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="var(--text-muted)" />
          </marker>
        </defs>

        <g transform={`translate(${PAD},${PAD})`}>
          {/* Edges */}
          {(edges || []).map((edge, idx) => {
            const src = positions[String(edge.sourceNodeId)];
            const tgt = positions[String(edge.targetNodeId)];
            if (!src || !tgt) return null;
            if (String(edge.sourceNodeId) === String(edge.targetNodeId)) return null;

            const srcCX = src.x + NODE_W / 2, srcCY = src.y + NODE_H / 2;
            const tgtCX = tgt.x + NODE_W / 2, tgtCY = tgt.y + NODE_H / 2;
            const dx = tgtCX - srcCX, dy = tgtCY - srcCY;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const nx = dx / len, ny = dy / len;

            const hasBidi = (edges || []).some(
              (e2) =>
                String(e2.sourceNodeId) === String(edge.targetNodeId) &&
                String(e2.targetNodeId) === String(edge.sourceNodeId)
            );

            const p1 = nodeExitPoint(srcCX, srcCY, nx, ny);
            const p2 = nodeExitPoint(tgtCX, tgtCY, -nx, -ny);

            let d;
            let labelX, labelY;
            if (hasBidi) {
              const sign = String(edge.sourceNodeId) < String(edge.targetNodeId) ? 1 : -1;
              const px = -ny * 16 * sign, py = nx * 16 * sign;
              const mx = (p1.x + p2.x) / 2 + px, my = (p1.y + p2.y) / 2 + py;
              d = `M ${p1.x},${p1.y} Q ${mx},${my} ${p2.x},${p2.y}`;
              labelX = mx; labelY = my;
            } else {
              // Smooth cubic bezier along the dominant axis (Signal skin); the
              // curve's midpoint is exactly the straight-line midpoint, so
              // label anchoring is unchanged.
              const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
              d = Math.abs(dy) >= Math.abs(dx)
                ? `M ${p1.x},${p1.y} C ${p1.x},${my} ${p2.x},${my} ${p2.x},${p2.y}`
                : `M ${p1.x},${p1.y} C ${mx},${p1.y} ${mx},${p2.y} ${p2.x},${p2.y}`;
              labelX = mx; labelY = my;
            }

            const tone = edgeToneFor(edge);
            const protocol = edge.protocol || "";
            const description = edge.description || "";
            // Descriptions may be multi-line (e.g. "Source 1.2.3.4\nNAT
            // 5.6.7.8"); each line truncates independently and stacks below.
            const descLines = description
              ? description.split("\n").map((l) => (l.length > 32 ? l.slice(0, 31) + "…" : l))
              : [];
            const labelFill = tone === "danger"
              ? "var(--danger)"
              : tone === "warn" ? "var(--warn)" : "var(--text-muted)";

            const edgeTip = [protocol, edge.scope, description.replace(/\n/g, " · ")]
              .filter(Boolean).join(" · ");

            return (
              <g key={edge.id ?? `e${idx}`}>
                {edgeTip ? <title>{edgeTip}</title> : null}
                <path d={d}
                  className={`sg-edge${tone === "ok" ? "" : ` sg-edge--${tone}`}`}
                  markerEnd={`url(#topo-arrow-${tone})`} />
                {protocol ? (
                  <text x={labelX} y={labelY - 5} textAnchor="middle"
                    fill={labelFill} fontSize={10} fontWeight={600}
                    style={{ paintOrder: "stroke" }} stroke="var(--bg-inset)" strokeWidth={3}>
                    {protocol}
                  </text>
                ) : null}
                {descLines.map((line, i) => (
                  <text key={i} x={labelX} y={labelY + (protocol ? 7 : -2) + i * 10} textAnchor="middle"
                    fill="var(--text-muted)" fontSize={9}
                    style={{ paintOrder: "stroke" }} stroke="var(--bg-inset)" strokeWidth={3}>
                    {line}
                  </text>
                ))}
              </g>
            );
          })}

          {/* Nodes — white rounded cards: 3px status bar, tinted icon chip,
              bold name + tiny mono sub-line (Signal topo skin). */}
          {(nodes || []).map((node) => {
            const pos = positions[String(node.id)];
            if (!pos) return null;
            const sub = node.overlay === "transport"
              ? (node.transportName || node.type || "")
              : (node.type || "");
            const hasSub = Boolean(sub);
            const label = truncate(node.name, 15);
            const subLabel = truncate(sub, 20);

            const barTone = node.componentStatus
              ? (STATUS_TONE[node.componentStatus] || "muted")
              : "muted";
            const chip = CHIP_COLORS[chipToneFor(node)];
            const glyphKind = node.overlay === "client" || node.overlay === "transport"
              ? node.overlay
              : "component";

            const chipCX = pos.x + 27, chipCY = pos.y + NODE_H / 2;
            const textX = pos.x + 46;
            const nameY = hasSub ? pos.y + 19 : pos.y + NODE_H / 2 + 1;

            return (
              <g key={node.id} className="topo-node" title={node.description || node.name}>
                {node.description ? <title>{node.description}</title> : null}
                <rect className="topo-node-card" x={pos.x} y={pos.y} width={NODE_W} height={NODE_H}
                  rx={14} ry={14}
                  fill="var(--bg-panel)" stroke="var(--border-strong)" strokeWidth={1}
                  filter="url(#topo-shadow)" />
                <rect x={pos.x + 6} y={pos.y + 9} width={3} height={NODE_H - 18} rx={1.5}
                  fill={BAR_FILL[barTone]}>
                  {node.componentStatus ? (
                    <title>{`Health: ${node.componentStatus}`}</title>
                  ) : null}
                </rect>
                <circle cx={chipCX} cy={chipCY} r={13} fill={chip.bg} />
                <NodeGlyph kind={glyphKind} cx={chipCX} cy={chipCY} color={chip.fg} />
                <text x={textX} y={nameY}
                  textAnchor="start" dominantBaseline="middle"
                  fill="var(--text-strong)" fontSize={12} fontWeight={700}>
                  {label}
                </text>
                {hasSub && (
                  <text x={textX} y={pos.y + 33}
                    textAnchor="start" dominantBaseline="middle"
                    fill="var(--text-muted)" fontSize={8.5} fontFamily="var(--font-mono)">
                    {subLabel}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}
