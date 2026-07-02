import { useMemo } from "react";

// Shared, read-only topology renderer used by the Application Services detail
// view and the Client Service Access Topology overlay. The layout helpers are
// exported so the interactive Topology editor can reuse the same geometry.

// Component health → topology node indicator color.
export const TOPO_STATUS_COLOR = {
  healthy: "#22c55e",
  degraded: "#f59e0b",
  unhealthy: "#ef4444",
  unknown: "#64748b",
};

// Accent colors for the client-access overlay node types.
const OVERLAY_ACCENT = {
  client: "#38bdf8",
  transport: "#a78bfa",
  service: "#22c55e",
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

  const ids = nodes.map((nd) => String(nd.id));

  // Place nodes initially on a circle so no two start at the same point
  const initR = Math.max(160, (n * (NODE_W + 50)) / (2 * Math.PI));
  const pos = {}, vel = {};
  ids.forEach((id, i) => {
    const angle = (2 * Math.PI * i / n) - Math.PI / 2;
    pos[id] = { x: initR * Math.cos(angle), y: initR * Math.sin(angle) };
    vel[id] = { x: 0, y: 0 };
  });

  const REPEL = 22000;
  const SPRING_K = 0.07;
  const SPRING_LEN = Math.max(NODE_W * 2.4, 300);

  for (let iter = 0; iter < 200; iter++) {
    const cool = Math.pow(1 - iter / 200, 2);

    // Repulsion between every node pair
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = pos[ids[i]], b = pos[ids[j]];
        const dx = b.x - a.x, dy = b.y - a.y;
        const d2 = dx * dx + dy * dy || 0.01;
        const d = Math.sqrt(d2);
        const f = REPEL / d2;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        vel[ids[i]].x -= fx; vel[ids[i]].y -= fy;
        vel[ids[j]].x += fx; vel[ids[j]].y += fy;
      }
    }

    // Spring attraction along edges (count bidi pairs once)
    const seen = new Set();
    edges.forEach((e) => {
      const s = String(e.sourceNodeId), t = String(e.targetNodeId);
      if (s === t || !pos[s] || !pos[t]) return;
      const key = s < t ? `${s}|${t}` : `${t}|${s}`;
      if (seen.has(key)) return;
      seen.add(key);
      const dx = pos[t].x - pos[s].x, dy = pos[t].y - pos[s].y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.1;
      const f = SPRING_K * (d - SPRING_LEN);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      vel[s].x += fx; vel[s].y += fy;
      vel[t].x -= fx; vel[t].y -= fy;
    });

    // Integrate with cooling
    ids.forEach((id) => {
      pos[id].x += vel[id].x * cool;
      pos[id].y += vel[id].y * cool;
      vel[id].x *= 0.65;
      vel[id].y *= 0.65;
    });
  }

  // Shift to (0,0) origin
  const xs = ids.map((id) => pos[id].x), ys = ids.map((id) => pos[id].y);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const maxX = Math.max(...xs), maxY = Math.max(...ys);

  const positions = {};
  ids.forEach((id) => {
    positions[id] = { x: pos[id].x - minX, y: pos[id].y - minY };
  });

  return {
    positions,
    svgW: (maxX - minX) + NODE_W + PAD * 2,
    svgH: (maxY - minY) + NODE_H + PAD * 2,
  };
}

export default function TopologyViewer({ nodes, edges, compact = false }) {
  const { positions, svgW, svgH } = useMemo(
    () => computeLayout(nodes || [], edges || []),
    [nodes, edges]
  );

  if (!nodes || nodes.length === 0) {
    return <p className="muted" style={{ fontSize: "0.875rem", fontStyle: "italic" }}>No topology defined yet.</p>;
  }

  // Zoom out a touch by padding the viewBox with extra margin around the
  // content, so the topology renders smaller and centered in its container.
  const zoomMx = svgW * 0.12;
  const zoomMy = svgH * 0.12;
  // Never scale the SVG *up* past its natural size — otherwise a 1–2 node graph
  // gets stretched to the full container width and the nodes balloon. Cap the
  // rendered width to the viewBox width (treated as px) and center it; larger
  // graphs still scale down to fit.
  const naturalWidth = Math.round(svgW + zoomMx * 2);

  return (
    <div className={`topo-viewer${compact ? " topo-viewer--compact" : ""}`}>
      <svg viewBox={`${-zoomMx} ${-zoomMy} ${svgW + zoomMx * 2} ${svgH + zoomMy * 2}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ display: "block", width: "100%", maxWidth: `${naturalWidth}px`, height: "auto", margin: "0 auto" }}>
        <defs>
          <filter id="topo-shadow" x="-30%" y="-30%" width="160%" height="160%">
            <feDropShadow dx="0" dy="2" stdDeviation="4" floodColor="rgba(0,0,0,0.5)" />
          </filter>
          <marker id="topo-arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="#64748b" />
          </marker>
          <marker id="topo-arrow-ext" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="#f5b945" />
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
            const stroke = isExternal ? "#f5b945" : "#475569";
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
                    fill={isExternal ? "#f5b945" : "#94a3b8"} fontSize={10} fontWeight={600}
                    style={{ paintOrder: "stroke" }} stroke="#0b1120" strokeWidth={3}>
                    {protocol}
                  </text>
                ) : null}
                {description ? (
                  <text x={labelX} y={labelY + (protocol ? 7 : -2)} textAnchor="middle"
                    fill="#94a3b8" fontSize={9}
                    style={{ paintOrder: "stroke" }} stroke="#0b1120" strokeWidth={3}>
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
            const strokeColor = overlayAccent || statusColor || "#334155";

            return (
              <g key={node.id} title={node.description || node.name}>
                {node.description ? <title>{node.description}</title> : null}
                <rect x={pos.x} y={pos.y} width={NODE_W} height={NODE_H}
                  rx={8} ry={8}
                  fill={overlayAccent ? "#0f1e33" : "#1e293b"} stroke={strokeColor} strokeWidth={overlayAccent || statusColor ? 2 : 1.5}
                  filter="url(#topo-shadow)" />
                {statusColor && !overlayAccent && (
                  <circle cx={pos.x + NODE_W - 11} cy={pos.y + 11} r={4} fill={statusColor}>
                    <title>{`Component health: ${node.componentStatus}`}</title>
                  </circle>
                )}
                {hasType && (
                  <text x={pos.x + NODE_W / 2} y={typeY}
                    textAnchor="middle" dominantBaseline="middle"
                    fill={overlayAccent || "#64748b"} fontSize={10} fontWeight={500} letterSpacing="0.06em">
                    {typeLabel?.toUpperCase()}
                  </text>
                )}
                <text x={pos.x + NODE_W / 2} y={nameY}
                  textAnchor="middle" dominantBaseline="middle"
                  fill="#e2e8f0" fontSize={13} fontWeight={600}>
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
