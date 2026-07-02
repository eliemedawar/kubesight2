import { useMemo } from "react";

// Shared, read-only topology renderer used by the Application Services detail
// view and the Client Service Access Topology overlay. The layout helpers are
// exported so the interactive Topology editor can reuse the same geometry.

// Component health → topology node indicator color.
export const TOPO_STATUS_COLOR = {
  healthy: "var(--ok)",
  degraded: "var(--warn)",
  unhealthy: "var(--danger)",
  unknown: "var(--text-muted)",
};

// Accent colors for the client-access overlay node types.
const OVERLAY_ACCENT = {
  client: "var(--info)",
  transport: "var(--ai)",
  service: "var(--ok)",
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

  // Bucket nodes into layers by rank (input order preserved within each layer).
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

export default function TopologyViewer({ nodes, edges, compact = false, fillWidth = false }) {
  const { positions, svgW, svgH } = useMemo(
    () => computeLayout(nodes || [], edges || []),
    [nodes, edges]
  );

  if (!nodes || nodes.length === 0) {
    return <p className="muted" style={{ fontSize: "0.875rem", fontStyle: "italic" }}>No topology defined yet.</p>;
  }

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

  return (
    <div className={`topo-viewer${compact ? " topo-viewer--compact" : ""}`}>
      <svg viewBox={`${-zoomMx} ${-zoomMy} ${svgW + zoomMx * 2} ${svgH + zoomMy * 2}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ display: "block", width: "100%", maxWidth: fillWidth ? "100%" : `${naturalWidth}px`, height: "auto", margin: "0 auto" }}>
        <defs>
          <filter id="topo-shadow" x="-30%" y="-30%" width="160%" height="160%">
            <feDropShadow dx="0" dy="2" stdDeviation="4" floodColor="rgba(0,0,0,0.5)" />
          </filter>
          <marker id="topo-arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="var(--text-muted)" />
          </marker>
          <marker id="topo-arrow-ext" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="var(--warn)" />
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
              d = `M ${p1.x},${p1.y} L ${p2.x},${p2.y}`;
              labelX = (p1.x + p2.x) / 2; labelY = (p1.y + p2.y) / 2;
            }

            const isExternal = edge.scope === "external";
            const stroke = isExternal ? "var(--warn)" : "var(--border-strong)";
            const protocol = edge.protocol || "";
            const description = edge.description || "";
            const descLabel = description.length > 32 ? description.slice(0, 31) + "…" : description;

            const edgeTip = [protocol, edge.scope, edge.description].filter(Boolean).join(" · ");

            return (
              <g key={edge.id ?? `e${idx}`}>
                {edgeTip ? <title>{edgeTip}</title> : null}
                <path d={d}
                  fill="none" stroke={stroke} strokeWidth={1.5}
                  strokeDasharray={isExternal ? "6 4" : undefined}
                  markerEnd={`url(#${isExternal ? "topo-arrow-ext" : "topo-arrow"})`} />
                {protocol ? (
                  <text x={labelX} y={labelY - 5} textAnchor="middle"
                    fill={isExternal ? "var(--warn)" : "var(--text-muted)"} fontSize={10} fontWeight={600}
                    style={{ paintOrder: "stroke" }} stroke="var(--bg-inset)" strokeWidth={3}>
                    {protocol}
                  </text>
                ) : null}
                {description ? (
                  <text x={labelX} y={labelY + (protocol ? 7 : -2)} textAnchor="middle"
                    fill="var(--text-muted)" fontSize={9}
                    style={{ paintOrder: "stroke" }} stroke="var(--bg-inset)" strokeWidth={3}>
                    {descLabel}
                  </text>
                ) : null}
              </g>
            );
          })}

          {/* Nodes */}
          {(nodes || []).map((node) => {
            const pos = positions[String(node.id)];
            if (!pos) return null;
            const hasType = Boolean(node.type);
            const nameY = hasType ? pos.y + 31 : pos.y + NODE_H / 2 + 1;
            const typeY = pos.y + 14;
            const label = node.name.length > 17 ? node.name.slice(0, 16) + "…" : node.name;
            const typeLabel = node.type && node.type.length > 16 ? node.type.slice(0, 15) + "…" : node.type;

            const overlayAccent = node.overlay ? OVERLAY_ACCENT[node.overlay] : null;
            const statusColor = node.componentStatus ? TOPO_STATUS_COLOR[node.componentStatus] : null;
            const strokeColor = overlayAccent || statusColor || "var(--border-strong)";

            return (
              <g key={node.id} title={node.description || node.name}>
                {node.description ? <title>{node.description}</title> : null}
                <rect x={pos.x} y={pos.y} width={NODE_W} height={NODE_H}
                  rx={8} ry={8}
                  fill={overlayAccent ? "var(--bg-panel-strong)" : "var(--bg-panel)"} stroke={strokeColor} strokeWidth={overlayAccent || statusColor ? 2 : 1.5}
                  filter="url(#topo-shadow)" />
                {statusColor && !overlayAccent && (
                  <circle cx={pos.x + NODE_W - 11} cy={pos.y + 11} r={4} fill={statusColor}>
                    <title>{`Component health: ${node.componentStatus}`}</title>
                  </circle>
                )}
                {hasType && (
                  <text x={pos.x + NODE_W / 2} y={typeY}
                    textAnchor="middle" dominantBaseline="middle"
                    fill={overlayAccent || "var(--text-muted)"} fontSize={10} fontWeight={500} letterSpacing="0.06em">
                    {typeLabel?.toUpperCase()}
                  </text>
                )}
                <text x={pos.x + NODE_W / 2} y={nameY}
                  textAnchor="middle" dominantBaseline="middle"
                  fill="var(--text-main)" fontSize={13} fontWeight={600}>
                  {label}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}
