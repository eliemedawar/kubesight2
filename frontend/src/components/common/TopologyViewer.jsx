import { useEffect, useMemo, useRef, useState } from "react";

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

  // Overlay client nodes rank 0 by default (nothing points at them). When a
  // client's tunnel attaches deep inside the service graph, pull the client
  // down beside that component so the tunnel spans a single layer instead of
  // sweeping across the whole drawing (and dragging its labels over other
  // nodes). Outbound tunnels (component → client) already rank the client
  // below the component naturally.
  nodes.forEach((nd) => {
    if (nd.overlay !== "client") return;
    const cid = String(nd.id);
    const targetRanks = edges
      .filter((e) =>
        e.kind === "tunnel" &&
        String(e.sourceNodeId) === cid &&
        idSet.has(String(e.targetNodeId)))
      .map((e) => rank[String(e.targetNodeId)]);
    if (targetRanks.length) rank[cid] = Math.max(0, Math.min(...targetRanks) - 1);
  });

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

export default function TopologyViewer({
  nodes,
  edges,
  compact = false,
  fillWidth = false,
  allowFullscreen = true,
  zoomable = false,
  onNodeClick,
  nodeClickable,
}) {
  const { positions, svgW, svgH } = useMemo(
    () => computeLayout(nodes || [], edges || []),
    [nodes, edges]
  );

  const nodeById = useMemo(() => {
    const map = {};
    (nodes || []).forEach((n) => { map[String(n.id)] = n; });
    return map;
  }, [nodes]);

  // Parallel edges between the same unordered node pair (A→B twice, or A→B
  // plus B→A): group them so each can fan out into its own lane instead of
  // drawing on top of the others. Tunnels keep their own geometry.
  const pairGroups = useMemo(() => {
    const g = {};
    (edges || []).forEach((e, i) => {
      if (e.kind === "tunnel") return;
      const a = String(e.sourceNodeId), b = String(e.targetNodeId);
      const key = a < b ? `${a}|${b}` : `${b}|${a}`;
      (g[key] = g[key] || []).push(i);
    });
    return g;
  }, [edges]);

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

  // ── Pan & zoom (opt-in via `zoomable`) ─────────────────────────────────
  const svgRef = useRef(null);
  const panRef = useRef(null);
  const draggedRef = useRef(false);
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const [panning, setPanning] = useState(false);

  // A new graph re-fits via the viewBox; drop any prior pan/zoom so it starts centered.
  useEffect(() => {
    setView({ k: 1, x: 0, y: 0 });
  }, [nodes, edges]);

  // Wheel-to-zoom toward the cursor. A native, non-passive listener so the page
  // doesn't scroll while the pointer is over the canvas.
  useEffect(() => {
    if (!zoomable) return undefined;
    const svg = svgRef.current;
    if (!svg) return undefined;
    const onWheel = (event) => {
      event.preventDefault();
      const ctm = svg.getScreenCTM();
      if (!ctm) return;
      const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(ctm.inverse());
      setView((v) => {
        const k = Math.min(5, Math.max(0.3, v.k * Math.exp(-event.deltaY * 0.0015)));
        const ratio = k / v.k;
        return { k, x: point.x - ratio * (point.x - v.x), y: point.y - ratio * (point.y - v.y) };
      });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [zoomable]);

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
  const svgStyle =
    fullscreen || zoomable
      ? // Fill the container so pan/zoom has room; the viewBox letterboxes the
        // graph (contain) and keeps it centered.
        { display: "block", width: "100%", height: "100%" }
      : { display: "block", width: "100%", maxWidth: fillWidth ? "100%" : `${naturalWidth}px`, height: "auto", margin: "0 auto" };

  // Drag-to-pan. Capture is engaged *lazily* — only once the pointer moves past
  // a small threshold — so a plain click still reaches the node underneath
  // (capturing on pointerdown would redirect the click to the SVG and swallow
  // node drill-downs). Deltas convert screen px → viewBox-user units via the
  // CTM scale, so panning tracks the cursor at any zoom level.
  const startPan = (event) => {
    if (!zoomable || (event.button && event.button !== 0)) return;
    const ctm = svgRef.current?.getScreenCTM();
    panRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      origX: view.x,
      origY: view.y,
      ax: ctm ? ctm.a : 1,
      ay: ctm ? ctm.d : 1,
      pointerId: event.pointerId,
      moved: false,
      capturing: false,
    };
  };
  const movePan = (event) => {
    const pan = panRef.current;
    if (!pan) return;
    const dxPx = event.clientX - pan.startX;
    const dyPx = event.clientY - pan.startY;
    if (!pan.capturing && Math.abs(dxPx) < 4 && Math.abs(dyPx) < 4) return; // still a click
    if (!pan.capturing) {
      pan.capturing = true;
      pan.moved = true;
      setPanning(true);
      svgRef.current?.setPointerCapture?.(pan.pointerId);
    }
    setView((v) => ({ ...v, x: pan.origX + dxPx / (pan.ax || 1), y: pan.origY + dyPx / (pan.ay || 1) }));
  };
  const endPan = () => {
    const pan = panRef.current;
    if (!pan) return;
    if (pan.capturing) svgRef.current?.releasePointerCapture?.(pan.pointerId);
    panRef.current = null;
    setPanning(false);
    // Flag the just-ended gesture as a drag so the ensuing click on a node is
    // ignored; cleared on the next tick, after the click has been dispatched.
    draggedRef.current = pan.moved;
    window.setTimeout(() => { draggedRef.current = false; }, 0);
  };
  const zoomBy = (factor) =>
    setView((v) => {
      const k = Math.min(5, Math.max(0.3, v.k * factor));
      const ratio = k / v.k;
      const cx = svgW / 2;
      const cy = svgH / 2;
      return { k, x: cx - ratio * (cx - v.x), y: cy - ratio * (cy - v.y) };
    });
  const resetView = () => setView({ k: 1, x: 0, y: 0 });

  const isClickable = (node) =>
    onNodeClick ? (nodeClickable ? nodeClickable(node) : true) : false;
  const handleNodeClick = (node) => {
    if (draggedRef.current) return;
    if (isClickable(node)) onNodeClick(node);
  };

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
      {zoomable ? (
        <div className="topo-zoom" role="group" aria-label="Zoom controls">
          <button type="button" className="topo-fs-btn" onClick={() => zoomBy(1.25)} aria-label="Zoom in" title="Zoom in">
            <svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M10 5v10M5 10h10" /></svg>
          </button>
          <button type="button" className="topo-fs-btn" onClick={() => zoomBy(0.8)} aria-label="Zoom out" title="Zoom out">
            <svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M5 10h10" /></svg>
          </button>
          <button type="button" className="topo-fs-btn" onClick={resetView} aria-label="Reset view" title="Reset view">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7M3 4v4h4" /></svg>
          </button>
        </div>
      ) : null}
      <svg ref={svgRef}
        viewBox={`${-zoomMx} ${-zoomMy} ${svgW + zoomMx * 2} ${svgH + zoomMy * 2}`}
        preserveAspectRatio="xMidYMid meet"
        onPointerDown={startPan}
        onPointerMove={movePan}
        onPointerUp={endPan}
        onPointerLeave={endPan}
        style={zoomable ? { ...svgStyle, cursor: panning ? "grabbing" : "grab", touchAction: "none" } : svgStyle}>
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
          {/* Tunnel arrowhead: auto-start-reverse so the same marker points
              outward on both ends of a bidirectional tunnel. */}
          <marker id="topo-arrow-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto-start-reverse">
            <polygon points="0 0, 8 3, 0 6" fill="var(--accent-strong)" />
          </marker>
        </defs>

        <g transform={zoomable ? `translate(${view.x} ${view.y}) scale(${view.k})` : undefined}>
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

            const isTunnel = edge.kind === "tunnel";
            const aId = String(edge.sourceNodeId), bId = String(edge.targetNodeId);
            const group = isTunnel
              ? null
              : pairGroups[aId < bId ? `${aId}|${bId}` : `${bId}|${aId}`];
            const lane = group && group.length > 1
              ? group.indexOf(idx) - (group.length - 1) / 2
              : 0;

            const p1 = nodeExitPoint(srcCX, srcCY, nx, ny);
            const p2 = nodeExitPoint(tgtCX, tgtCY, -nx, -ny);

            let d;
            let labelX, labelY;
            let labelAnchor = "middle";
            let c1x, c1y, c2x, c2y; // cubic control points (smooth branch only)
            if (lane !== 0) {
              // The perpendicular is taken along the pair's canonical
              // direction (low id → high id) so opposite-direction edges land
              // in different lanes instead of mirroring onto each other. Each
              // label sits just outside its own arc, anchored outward, so the
              // lanes' labels never collide.
              const canon = aId < bId ? 1 : -1;
              const ox = -ny * 22 * lane * canon, oy = nx * 22 * lane * canon;
              const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
              d = `M ${p1.x},${p1.y} Q ${mx + ox * 2},${my + oy * 2} ${p2.x},${p2.y}`;
              labelX = mx + ox * 1.35; labelY = my + oy * 1.35;
              if (Math.abs(ox) > Math.abs(oy)) labelAnchor = ox < 0 ? "end" : "start";
            } else {
              // Smooth cubic bezier along the dominant axis (Signal skin); the
              // curve's midpoint is exactly the straight-line midpoint, so
              // label anchoring is unchanged.
              const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
              if (Math.abs(dy) >= Math.abs(dx)) {
                c1x = p1.x; c1y = my; c2x = p2.x; c2y = my;
              } else {
                c1x = mx; c1y = p1.y; c2x = mx; c2y = p2.y;
              }
              d = `M ${p1.x},${p1.y} C ${c1x},${c1y} ${c2x},${c2y} ${p2.x},${p2.y}`;
              labelX = mx; labelY = my;
            }

            if (isTunnel) {
              // Transport tunnel: a hollow pipe drawn straight from origin to
              // destination, with the transport type in a pill at the middle,
              // the Source IP label at the origin mouth and the Destination IP
              // label at the far mouth. The pipe is a wide accent stroke with
              // a background-colored stroke overdrawn to hollow it out, plus a
              // dashed centerline carrying the direction arrowhead(s).
              const bez = (a, b, c, e, t) => {
                const u = 1 - t;
                return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * e;
              };
              const anchor = (t) => ({
                x: bez(p1.x, c1x, c2x, p2.x, t),
                y: bez(p1.y, c1y, c2y, p2.y, t),
              });
              const srcA = anchor(0.18), tgtA = anchor(0.82);
              const toLines = (s) => String(s || "").split("\n").filter(Boolean)
                .map((l) => (l.length > 32 ? l.slice(0, 31) + "…" : l));
              const srcLines = toLines(edge.sourceLabel);
              const tgtLines = toLines(edge.targetLabel);
              const tType = edge.transportType || "Link";
              const tName = edge.transportName || "";
              const pillLabel = truncate(tType, 16);
              const pillW = Math.max(44, pillLabel.length * 6.4 + 18);
              // IP labels sit beside the pipe (right of vertical tunnels,
              // above horizontal ones) so their text halos don't erase it —
              // and are clamped clear of the mid-pill, which is wide enough
              // ("Leased Line") to cover them on short tunnels otherwise.
              const vertical = Math.abs(dy) >= Math.abs(dx);
              const subH = tName ? 11 : 0; // transport-name line under the pill
              let srcPos, tgtPos; // (i) => props of the block's i-th line
              if (vertical) {
                const yBlock = (a, lines, isUpper) => {
                  const first = isUpper
                    ? Math.min(a.y, labelY - 14 - (lines.length - 1) * 10)
                    : Math.max(a.y - (lines.length - 1) * 10, labelY + 20 + subH);
                  return (i) => ({ x: a.x + 13, y: first + i * 10, textAnchor: "start" });
                };
                const srcUpper = p1.y <= p2.y;
                srcPos = yBlock(srcA, srcLines, srcUpper);
                tgtPos = yBlock(tgtA, tgtLines, !srcUpper);
              } else {
                // Horizontal/diagonal tunnels: each IP block hugs its own end
                // of the pipe — Source beside the origin node, Destination
                // beside the target — anchored a third of the way along the
                // pipe on the side it is bending away from, so the text stays
                // off the pipe's path and clear of the node cards.
                const rightward = p2.x >= p1.x;
                const flatLift = Math.abs(dy) < 24 ? 16 : 0;
                const srcB = anchor(0.3), tgtB = anchor(0.7);
                srcPos = (i) => ({
                  x: srcB.x + (rightward ? -10 : 10),
                  y: srcB.y + i * 10 - flatLift,
                  textAnchor: rightward ? "end" : "start",
                });
                tgtPos = (i) => ({
                  x: tgtB.x + (rightward ? 10 : -10),
                  y: tgtB.y - (tgtLines.length - 1 - i) * 10 - flatLift,
                  textAnchor: rightward ? "start" : "end",
                });
              }
              const tip = [tType, tName, (edge.description || "").replace(/\n/g, " · ")]
                .filter(Boolean).join(" · ");
              const halo = { paintOrder: "stroke" };
              return (
                <g key={edge.id ?? `e${idx}`} className="topo-tunnel">
                  <title>{tip}</title>
                  <path d={d} fill="none" stroke="var(--accent-border)" strokeWidth={15} />
                  <path d={d} fill="none" stroke="var(--bg-inset)" strokeWidth={12} />
                  <path d={d} fill="none" stroke="var(--accent-strong)" strokeWidth={1.5}
                    strokeDasharray="6 5"
                    markerEnd="url(#topo-arrow-accent)"
                    markerStart={edge.bidirectional ? "url(#topo-arrow-accent)" : undefined} />
                  <rect x={labelX - pillW / 2} y={labelY - 10} width={pillW} height={20}
                    rx={10} ry={10}
                    fill="var(--bg-panel)" stroke="var(--accent-border)" strokeWidth={1}
                    filter="url(#topo-shadow)" />
                  <text x={labelX} y={labelY + 1} textAnchor="middle" dominantBaseline="middle"
                    fill="var(--accent-strong)" fontSize={9.5} fontWeight={750}
                    letterSpacing="0.05em">
                    {pillLabel}
                  </text>
                  {tName ? (
                    <text x={labelX} y={labelY + 19} textAnchor="middle"
                      fill="var(--text-muted)" fontSize={8.5} fontFamily="var(--font-mono)"
                      style={halo} stroke="var(--bg-inset)" strokeWidth={3}>
                      {truncate(tName, 20)}
                    </text>
                  ) : null}
                  {srcLines.map((line, i) => (
                    <text key={`s${i}`} {...srcPos(i)}
                      fill="var(--text-muted)" fontSize={9}
                      style={halo} stroke="var(--bg-inset)" strokeWidth={3}>
                      {line}
                    </text>
                  ))}
                  {tgtLines.map((line, i) => (
                    <text key={`t${i}`} {...tgtPos(i)}
                      fill="var(--text-muted)" fontSize={9}
                      style={halo} stroke="var(--bg-inset)" strokeWidth={3}>
                      {line}
                    </text>
                  ))}
                </g>
              );
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
                  <text x={labelX} y={labelY - 5} textAnchor={labelAnchor}
                    fill={labelFill} fontSize={10} fontWeight={600}
                    style={{ paintOrder: "stroke" }} stroke="var(--bg-inset)" strokeWidth={3}>
                    {protocol}
                  </text>
                ) : null}
                {descLines.map((line, i) => (
                  <text key={i} x={labelX} y={labelY + (protocol ? 7 : -2) + i * 10} textAnchor={labelAnchor}
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
            const clickableNode = isClickable(node);

            return (
              <g
                key={node.id}
                className={`topo-node${clickableNode ? " topo-node--click" : ""}`}
                onClick={clickableNode ? () => handleNodeClick(node) : undefined}
                style={clickableNode ? { cursor: "pointer" } : undefined}
                title={node.description || node.name}
              >
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
        </g>
      </svg>
    </div>
  );
}
