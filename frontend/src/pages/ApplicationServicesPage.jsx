import { useEffect, useMemo, useRef, useState } from "react";
import {
  createApplicationService,
  deleteApplicationService,
  listApplicationServices,
  listComponents,
  listNamespacesByCluster,
  listPickerWorkloads,
  updateApplicationService,
} from "../api";
import { useAuth } from "../context/AuthContext";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import SearchableSelect from "../components/common/SearchableSelect.jsx";

// Supported workload kinds for linked resources, DR counterparts and topology.
const WORKLOAD_KINDS = ["deployment", "statefulset", "daemonset", "pod"];
const KIND_LABEL = {
  deployment: "Deployment",
  statefulset: "StatefulSet",
  daemonset: "DaemonSet",
  pod: "Pod",
};

// Component health → topology node indicator color.
const TOPO_STATUS_COLOR = {
  healthy: "#22c55e",
  degraded: "#f59e0b",
  unhealthy: "#ef4444",
  unknown: "#64748b",
};

// ─── Health badge ────────────────────────────────────────────────────────────

const HEALTH_BADGE = { healthy: "pass", warning: "warning", critical: "fail", unknown: "pending" };

function HealthBadge({ health }) {
  const variant = HEALTH_BADGE[health] || "pending";
  return <span className={`status-badge status-badge--${variant}`}>{health || "unknown"}</span>;
}

// DR status reuses the same health vocabulary (healthy/warning/critical/unknown).
// A service with no DR counterparts linked shows a muted "No DR" pill.
function DrBadge({ hasDr, drHealth }) {
  if (!hasDr) {
    return <span className="status-badge status-badge--pending">No DR</span>;
  }
  return <HealthBadge health={drHealth || "unknown"} />;
}

// ─── Deployment picker components ────────────────────────────────────────────

function DeploymentRow({ dep, clusterName, onRemove }) {
  const kind = dep.kind || "deployment";
  return (
    <div className="dep-picker-row">
      <span className={`dep-picker-kind dep-picker-kind--${kind}`}>{KIND_LABEL[kind] || kind}</span>
      <span className="dep-picker-cluster">{clusterName || dep.clusterId}</span>
      <span className="dep-picker-sep">/</span>
      <span className="dep-picker-ns">{dep.namespace}</span>
      <span className="dep-picker-sep">/</span>
      <span className="dep-picker-name">{dep.deploymentName}</span>
      {onRemove && (
        <button type="button" className="btn-ghost dep-picker-remove" onClick={onRemove}>✕</button>
      )}
    </div>
  );
}

function DeploymentPicker({ deployments, onChange, canEdit, clusters = [] }) {
  const [namespaces, setNamespaces] = useState([]);
  const [pickerItems, setPickerItems] = useState([]);
  const [pickerCluster, setPickerCluster] = useState("");
  const [pickerNamespace, setPickerNamespace] = useState("");
  const [pickerKind, setPickerKind] = useState("deployment");
  const [nsLoading, setNsLoading] = useState(false);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [pickerError, setPickerError] = useState("");

  const fetchItems = async (cluster, namespace, kind) => {
    if (!cluster || !namespace) return;
    setItemsLoading(true);
    setPickerError("");
    try {
      const res = await listPickerWorkloads(cluster, namespace, kind);
      setPickerItems(res.items || []);
    } catch (err) {
      setPickerError(err.message || `Failed to load ${kind}s`);
    } finally { setItemsLoading(false); }
  };

  const handleClusterChange = async (val) => {
    setPickerCluster(val);
    setPickerNamespace("");
    setNamespaces([]);
    setPickerItems([]);
    setPickerError("");
    if (!val) return;
    setNsLoading(true);
    try {
      const res = await listNamespacesByCluster(val);
      setNamespaces(res.items || res.namespaces || res || []);
    } catch (err) {
      setPickerError(err.message || "Failed to load namespaces");
    } finally { setNsLoading(false); }
  };

  const handleNamespaceChange = (val) => {
    setPickerNamespace(val);
    setPickerItems([]);
    setPickerError("");
    fetchItems(pickerCluster, val, pickerKind);
  };

  const handleKindChange = (kind) => {
    setPickerKind(kind);
    setPickerItems([]);
    setPickerError("");
    if (pickerCluster && pickerNamespace) fetchItems(pickerCluster, pickerNamespace, kind);
  };

  const addDeployment = (depName) => {
    const entry = { clusterId: pickerCluster, namespace: pickerNamespace, deploymentName: depName, kind: pickerKind };
    if (!deployments.some((d) => d.clusterId === entry.clusterId && d.namespace === entry.namespace && d.deploymentName === entry.deploymentName && (d.kind || "deployment") === entry.kind)) {
      onChange([...deployments, entry]);
    }
  };

  const clusterNameById = Object.fromEntries(clusters.map((c) => [c.id, c.name || c.id]));
  const nsOptions = namespaces.map((n) => (typeof n === "string" ? n : n.name));
  // Resources in the picked namespace not already linked (for the same kind).
  const availablePickerItems = pickerItems.filter(
    (name) =>
      !deployments.some(
        (d) =>
          d.clusterId === pickerCluster &&
          d.namespace === pickerNamespace &&
          d.deploymentName === name &&
          (d.kind || "deployment") === pickerKind
      )
  );

  return (
    <div className="dep-picker">
      <div className="dep-picker-current">
        {deployments.length === 0 ? (
          <p className="muted" style={{ fontSize: "0.875rem" }}>None linked yet.</p>
        ) : (
          deployments.map((dep, idx) => (
            <DeploymentRow
              key={`${dep.kind || "deployment"}/${dep.clusterId}/${dep.namespace}/${dep.deploymentName}`}
              dep={dep}
              clusterName={clusterNameById[dep.clusterId]}
              onRemove={canEdit ? () => onChange(deployments.filter((_, i) => i !== idx)) : null}
            />
          ))
        )}
      </div>
      {canEdit && (
        <div className="dep-picker-add">
          <p className="muted" style={{ fontSize: "0.8125rem", fontWeight: 500, marginBottom: "0.4rem" }}>Add resource</p>
          <div className="dep-picker-controls">
            <SearchableSelect
              options={WORKLOAD_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] }))}
              value={pickerKind}
              onChange={(e) => handleKindChange(e.target.value)}
              placeholder="Kind…"
            />
            <SearchableSelect
              options={clusters.map((c) => ({ value: c.id, label: c.name || c.id }))}
              value={pickerCluster}
              onChange={(e) => handleClusterChange(e.target.value)}
              placeholder="Select cluster…"
            />
            <SearchableSelect
              options={nsOptions.map((ns) => ({ value: ns, label: ns }))}
              value={pickerNamespace}
              onChange={(e) => handleNamespaceChange(e.target.value)}
              placeholder={nsLoading ? "Loading…" : "Select namespace…"}
              disabled={!pickerCluster || nsLoading}
            />
            <SearchableSelect
              options={availablePickerItems.map((name) => ({ value: name, label: name }))}
              value=""
              onChange={(e) => e.target.value && addDeployment(e.target.value)}
              placeholder={
                itemsLoading ? "Loading…" : !pickerNamespace ? `Select ${KIND_LABEL[pickerKind]}…` : `Add ${KIND_LABEL[pickerKind]}…`
              }
              disabled={!pickerNamespace || itemsLoading}
            />
          </div>
          {pickerError && <p className="banner-message error" style={{ marginTop: "0.5rem" }}>{pickerError}</p>}
          {pickerNamespace && !itemsLoading && pickerItems.length === 0 && (
            <p className="muted" style={{ fontSize: "0.85rem", marginTop: "0.4rem" }}>No {KIND_LABEL[pickerKind]}s found in this namespace.</p>
          )}
          {pickerNamespace && !itemsLoading && pickerItems.length > 0 && availablePickerItems.length === 0 && (
            <p className="muted" style={{ fontSize: "0.85rem", marginTop: "0.4rem" }}>All {KIND_LABEL[pickerKind]}s in this namespace are already linked.</p>
          )}
        </div>
      )}
    </div>
  );
}

// ─── DR linker (per-component disaster-recovery counterpart) ──────────────────

function DRLinker({ dr, onChange, clusters = [] }) {
  const [editing, setEditing] = useState(!dr);
  const [cluster, setCluster] = useState(dr?.clusterId || "");
  const [namespace, setNamespace] = useState(dr?.namespace || "");
  const [kind, setKind] = useState(dr?.kind || "deployment");
  const [namespaces, setNamespaces] = useState([]);
  const [items, setItems] = useState([]);
  const [nsLoading, setNsLoading] = useState(false);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [error, setError] = useState("");

  const clusterNameById = Object.fromEntries(clusters.map((c) => [c.id, c.name || c.id]));

  const loadNamespaces = async (val) => {
    if (!val) { setNamespaces([]); return; }
    setNsLoading(true);
    try {
      const res = await listNamespacesByCluster(val);
      setNamespaces((res.items || res.namespaces || res || []).map((n) => (typeof n === "string" ? n : n.name)));
    } catch (err) {
      setError(err.message || "Failed to load namespaces");
    } finally { setNsLoading(false); }
  };

  const loadItems = async (cl, ns, k) => {
    if (!cl || !ns) { setItems([]); return; }
    setItemsLoading(true);
    setError("");
    try {
      const res = await listPickerWorkloads(cl, ns, k);
      setItems(res.items || []);
    } catch (err) {
      setError(err.message || `Failed to load ${k}s`);
    } finally { setItemsLoading(false); }
  };

  const handleCluster = (val) => {
    setCluster(val); setNamespace(""); setItems([]); setNamespaces([]);
    loadNamespaces(val);
  };
  const handleNamespace = (val) => { setNamespace(val); setItems([]); loadItems(cluster, val, kind); };
  const handleKind = (k) => { setKind(k); setItems([]); if (cluster && namespace) loadItems(cluster, namespace, k); };

  const pick = (name) => {
    onChange({ clusterId: cluster, namespace, deploymentName: name, kind });
    setEditing(false);
  };

  const clearLink = () => { onChange(null); setEditing(true); setCluster(""); setNamespace(""); setItems([]); };

  if (dr && !editing) {
    return (
      <div className="dr-linker dr-linker--set">
        <DeploymentRow
          dep={{ clusterId: dr.clusterId, namespace: dr.namespace, deploymentName: dr.deploymentName, kind: dr.kind }}
          clusterName={clusterNameById[dr.clusterId]}
        />
        <div style={{ display: "flex", gap: "0.35rem", marginTop: "0.25rem" }}>
          <button type="button" className="btn-outline btn-compact" onClick={() => { setEditing(true); loadNamespaces(dr.clusterId); loadItems(dr.clusterId, dr.namespace, dr.kind); }}>Change</button>
          <button type="button" className="btn-ghost btn-compact danger" onClick={clearLink}>Remove DR</button>
        </div>
      </div>
    );
  }

  return (
    <div className="dr-linker dr-linker--edit">
      <div className="dep-picker-controls">
        <SearchableSelect
          options={WORKLOAD_KINDS.map((k) => ({ value: k, label: KIND_LABEL[k] }))}
          value={kind} onChange={(e) => handleKind(e.target.value)} placeholder="Kind…" />
        <SearchableSelect
          options={clusters.map((c) => ({ value: c.id, label: c.name || c.id }))}
          value={cluster} onChange={(e) => handleCluster(e.target.value)} placeholder="DR cluster…" />
        <SearchableSelect
          options={namespaces.map((n) => ({ value: n, label: n }))}
          value={namespace} onChange={(e) => handleNamespace(e.target.value)}
          placeholder={nsLoading ? "Loading…" : "DR namespace…"} disabled={!cluster || nsLoading} />
        <SearchableSelect
          options={items.map((name) => ({ value: name, label: name }))}
          value=""
          onChange={(e) => e.target.value && pick(e.target.value)}
          placeholder={itemsLoading ? "Loading…" : !namespace ? `Select ${KIND_LABEL[kind]}…` : `DR ${KIND_LABEL[kind]}…`}
          disabled={!namespace || itemsLoading} />
      </div>
      {error && <p className="banner-message error" style={{ marginTop: "0.4rem" }}>{error}</p>}
      {namespace && !itemsLoading && items.length === 0 && (
        <p className="muted" style={{ fontSize: "0.82rem", marginTop: "0.4rem" }}>No {KIND_LABEL[kind]}s found in this namespace.</p>
      )}
      {dr && (
        <button type="button" className="btn-ghost btn-compact danger" style={{ marginTop: "0.35rem" }} onClick={clearLink}>Remove DR</button>
      )}
    </div>
  );
}

// ─── Topology layout ─────────────────────────────────────────────────────────

const NODE_W = 160;
const NODE_H = 48;
const PAD = 40;

// Point where the ray from (cx,cy) toward (nx,ny) exits a rectangle of (w,h)
function rectExitPoint(cx, cy, nx, ny, w = NODE_W, h = NODE_H) {
  const tx = Math.abs(nx) < 1e-9 ? Infinity : (w / 2) / Math.abs(nx);
  const ty = Math.abs(ny) < 1e-9 ? Infinity : (h / 2) / Math.abs(ny);
  const t = Math.min(tx, ty);
  return { x: cx + nx * t, y: cy + ny * t };
}
function nodeExitPoint(cx, cy, nx, ny) {
  return rectExitPoint(cx, cy, nx, ny, NODE_W, NODE_H);
}

// A node's manually-saved position, if it has one.
function savedNodePos(n) {
  const x = typeof n.x === "number" ? n.x : n.positionX;
  const y = typeof n.y === "number" ? n.y : n.positionY;
  return typeof x === "number" && typeof y === "number" ? { x, y } : null;
}

function computeLayout(nodes, edges) {
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

// ─── Topology Viewer ─────────────────────────────────────────────────────────

function TopologyViewer({ nodes, edges, compact = false }) {
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

            const statusColor = node.componentStatus ? TOPO_STATUS_COLOR[node.componentStatus] : null;

            return (
              <g key={node.id} title={node.description || node.name}>
                <rect x={pos.x} y={pos.y} width={NODE_W} height={NODE_H}
                  rx={8} ry={8}
                  fill="#1e293b" stroke={statusColor || "#334155"} strokeWidth={statusColor ? 2 : 1.5}
                  filter="url(#topo-shadow)" />
                {statusColor && (
                  <circle cx={pos.x + NODE_W - 11} cy={pos.y + 11} r={4} fill={statusColor}>
                    <title>{`Component health: ${node.componentStatus}`}</title>
                  </circle>
                )}
                {hasType && (
                  <text x={pos.x + NODE_W / 2} y={typeY}
                    textAnchor="middle" dominantBaseline="middle"
                    fill="#64748b" fontSize={10} fontWeight={500} letterSpacing="0.06em">
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

// ─── Topology Editor (interactive canvas) ────────────────────────────────────

let _topoIdCounter = 1;
function newTempId() { return `node-new-${_topoIdCounter++}`; }

// Editor node box dimensions (must match .topo-canvas-node CSS).
const E_NODE_W = 184;
const E_NODE_H = 104;
const TOPO_CANVAS_MIN_H = 440;

// Connection metadata options.
const EDGE_PROTOCOLS = ["HTTP", "HTTPS", "gRPC", "TCP", "UDP", "WebSocket", "AMQP", "Kafka", "SQL", "Redis", "DNS"];
const DEFAULT_PROTOCOL = "HTTP";
const DEFAULT_SCOPE = "internal";

function edgeStroke(scope) {
  return scope === "external" ? "#f5b945" : "#64748b";
}

function TopologyEditor({ topology, onChange, components = [] }) {
  const { nodes, edges } = topology;
  const canvasRef = useRef(null);
  const dragRef = useRef(null);          // { tempId, offX, offY, moved, pointerId } while dragging
  const rafRef = useRef(0);              // pending requestAnimationFrame id for drag updates
  const pendingRef = useRef(null);       // latest canvas point during a drag
  const linkTargetRef = useRef(null);    // tempId currently hovered while linking
  const [linking, setLinking] = useState(null); // { fromTempId, x, y } while drawing an edge
  const [drag, setDrag] = useState(null);        // { tempId, x, y } uncommitted live drag position
  const [edgePopup, setEdgePopup] = useState(null); // { index, x, y } open connection editor

  const nodeById = useMemo(() => {
    const m = {};
    nodes.forEach((n) => { m[n.tempId] = n; });
    return m;
  }, [nodes]);

  // Live position of a node: the one being dragged uses the uncommitted position
  // so only this component re-renders during a drag (not the whole modal).
  const posOf = (node) =>
    drag && drag.tempId === node.tempId ? { x: drag.x, y: drag.y } : { x: node.x, y: node.y };

  // Auto-place any node that lacks a position (legacy/AI topologies, or first load).
  useEffect(() => {
    if (!nodes.length) return;
    if (nodes.every((n) => typeof n.x === "number" && typeof n.y === "number")) return;
    const layoutNodes = nodes.map((n) => ({ id: n.tempId, x: n.x, y: n.y }));
    const layoutEdges = edges.map((e) => ({ sourceNodeId: e.sourceTempId, targetNodeId: e.targetTempId }));
    const { positions } = computeLayout(layoutNodes, layoutEdges);
    const placed = nodes.map((n) => {
      if (typeof n.x === "number" && typeof n.y === "number") return n;
      const p = positions[n.tempId];
      return p ? { ...n, x: p.x, y: p.y } : { ...n, x: 40, y: 40 };
    });
    onChange({ nodes: placed, edges });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges]);

  // Clean up a pending animation frame on unmount.
  useEffect(() => () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); }, []);

  const canvasPoint = (evt) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: evt.clientX - rect.left + canvasRef.current.scrollLeft,
      y: evt.clientY - rect.top + canvasRef.current.scrollTop,
    };
  };

  // Next free drop position near the top-left, staggered by node count.
  const nextDropPos = () => {
    const i = nodes.length;
    return { x: 40 + (i % 4) * (E_NODE_W + 40), y: 40 + Math.floor(i / 4) * (E_NODE_H + 48) };
  };

  const addNode = () => {
    const { x, y } = nextDropPos();
    onChange({
      nodes: [...nodes, { tempId: newTempId(), name: "", type: "", description: "", x, y }],
      edges,
    });
  };

  // Add a node backed by a predefined component (e.g. "WAF").
  const addComponentNode = (componentId) => {
    const comp = components.find((c) => String(c.id) === String(componentId));
    if (!comp) return;
    const { x, y } = nextDropPos();
    onChange({
      nodes: [...nodes, {
        tempId: newTempId(),
        name: comp.name,
        type: comp.category || "Component",
        description: comp.description || "",
        componentId: comp.id,
        componentStatus: comp.lastStatus || "unknown",
        x, y,
      }],
      edges,
    });
  };

  const updateNode = (tempId, field, value) => {
    onChange({ nodes: nodes.map((n) => (n.tempId === tempId ? { ...n, [field]: value } : n)), edges });
  };

  const removeNode = (tempId) => {
    setEdgePopup(null);
    onChange({
      nodes: nodes.filter((n) => n.tempId !== tempId),
      edges: edges.filter((e) => e.sourceTempId !== tempId && e.targetTempId !== tempId),
    });
  };

  const removeEdge = (idx) => {
    setEdgePopup(null);
    onChange({ nodes, edges: edges.filter((_, i) => i !== idx) });
  };

  const updateEdge = (idx, patch) => {
    onChange({ nodes, edges: edges.map((e, i) => (i === idx ? { ...e, ...patch } : e)) });
  };

  // Geometry helpers (use live drag position so edges/labels follow the drag).
  const centerOfNode = (node) => { const p = posOf(node); return { x: p.x + E_NODE_W / 2, y: p.y + E_NODE_H / 2 }; };
  const handlePos = (node) => { const p = posOf(node); return { x: p.x + E_NODE_W, y: p.y + E_NODE_H / 2 }; };
  const edgeEndpoints = (s, t) => {
    const sc = centerOfNode(s), tc = centerOfNode(t);
    const dx = tc.x - sc.x, dy = tc.y - sc.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = dx / len, ny = dy / len;
    return {
      p1: rectExitPoint(sc.x, sc.y, nx, ny, E_NODE_W, E_NODE_H),
      p2: rectExitPoint(tc.x, tc.y, -nx, -ny, E_NODE_W, E_NODE_H),
    };
  };

  // ── Node dragging ──
  const onNodePointerDown = (evt, node) => {
    if (evt.button !== 0) return;
    evt.stopPropagation();
    setEdgePopup(null);
    const p = canvasPoint(evt);
    dragRef.current = { tempId: node.tempId, offX: p.x - node.x, offY: p.y - node.y, moved: false, pointerId: evt.pointerId };
    pendingRef.current = null;
    setDrag({ tempId: node.tempId, x: node.x, y: node.y });
    try { canvasRef.current?.setPointerCapture(evt.pointerId); } catch { /* not supported */ }
  };

  // ── Edge linking ──
  // No pointer capture here: linking relies on the target node's hover/up
  // events firing (capture would redirect them all to the canvas). Canvas
  // pointermove/up still fire via event bubbling from the nodes.
  const onHandlePointerDown = (evt, node) => {
    if (evt.button !== 0) return;
    evt.stopPropagation();
    setEdgePopup(null);
    const p = canvasPoint(evt);
    setLinking({ fromTempId: node.tempId, x: p.x, y: p.y });
    linkTargetRef.current = null;
  };

  const flushDrag = () => {
    rafRef.current = 0;
    const pt = pendingRef.current;
    const dr = dragRef.current;
    if (!pt || !dr) return;
    dr.moved = true;
    setDrag({ tempId: dr.tempId, x: Math.max(0, pt.x - dr.offX), y: Math.max(0, pt.y - dr.offY) });
  };

  const onCanvasPointerMove = (evt) => {
    const p = canvasPoint(evt);
    if (dragRef.current) {
      // Throttle drag updates to one per animation frame for smoothness.
      pendingRef.current = p;
      if (!rafRef.current) rafRef.current = requestAnimationFrame(flushDrag);
    } else if (linking) {
      setLinking((l) => (l ? { ...l, x: p.x, y: p.y } : l));
    }
  };

  const commitDrag = () => {
    const dr = dragRef.current;
    if (dr && dr.moved) {
      const pt = pendingRef.current;
      const x = pt ? Math.max(0, pt.x - dr.offX) : drag?.x;
      const y = pt ? Math.max(0, pt.y - dr.offY) : drag?.y;
      if (typeof x === "number" && typeof y === "number") {
        onChange({ nodes: nodes.map((n) => (n.tempId === dr.tempId ? { ...n, x, y } : n)), edges });
      }
    }
    dragRef.current = null;
    pendingRef.current = null;
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0; }
    setDrag(null);
  };

  const finishLinking = () => {
    const from = linking?.fromTempId;
    const to = linkTargetRef.current;
    if (from && to && from !== to) {
      const dup = edges.some((e) => e.sourceTempId === from && e.targetTempId === to);
      if (!dup) {
        const nextEdges = [...edges, { sourceTempId: from, targetTempId: to, protocol: DEFAULT_PROTOCOL, scope: DEFAULT_SCOPE }];
        onChange({ nodes, edges: nextEdges });
        // Open the connection editor on the freshly created edge.
        const s = nodeById[from], t = nodeById[to];
        if (s && t) {
          const { p1, p2 } = edgeEndpoints(s, t);
          setEdgePopup({ index: nextEdges.length - 1, x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 });
        }
      }
    }
    setLinking(null);
    linkTargetRef.current = null;
  };

  const onCanvasPointerUp = (evt) => {
    if (dragRef.current) {
      try { canvasRef.current?.releasePointerCapture(evt.pointerId); } catch { /* ignore */ }
      commitDrag();
    }
    if (linking) finishLinking();
  };

  // If the pointer leaves the canvas mid-link, cancel it. (Dragging uses pointer
  // capture, so this never fires during a drag.)
  const onCanvasPointerLeave = () => {
    if (linking) {
      setLinking(null);
      linkTargetRef.current = null;
    }
  };

  const placedNodes = nodes.filter((n) => typeof n.x === "number" && typeof n.y === "number");
  const contentW = placedNodes.reduce((m, n) => Math.max(m, posOf(n).x + E_NODE_W), 0) + 60;
  const contentH = placedNodes.reduce((m, n) => Math.max(m, posOf(n).y + E_NODE_H), 0) + 60;

  const popupEdge = edgePopup ? edges[edgePopup.index] : null;

  return (
    <div className="topo-editor">
      <div className="topo-editor-block-header">
        <span className="topo-editor-block-title">Topology canvas</span>
        <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
          {components.length > 0 && (
            <div style={{ minWidth: 190 }}>
              <SearchableSelect
                options={components.map((c) => ({
                  value: String(c.id),
                  label: c.category ? `${c.name} · ${c.category}` : c.name,
                }))}
                value=""
                onChange={(e) => e.target.value && addComponentNode(e.target.value)}
                placeholder="+ Add from Components…"
              />
            </div>
          )}
          <button type="button" className="btn-outline btn-compact" onClick={addNode}>+ Custom</button>
        </div>
      </div>
      <p className="muted topo-canvas-hint">
        Add a predefined building block from <strong>Components</strong>, a custom box, or link deployments/pods below
        (which appear here automatically). Drag a box by its header to move it. Drag from the
        <span className="topo-handle-legend">●</span> handle on the right of a box to another to connect them.
      </p>

      <div
        ref={canvasRef}
        className="topo-canvas"
        style={{ height: nodes.length ? Math.max(TOPO_CANVAS_MIN_H, contentH) : TOPO_CANVAS_MIN_H }}
        onPointerDown={() => setEdgePopup(null)}
        onPointerMove={onCanvasPointerMove}
        onPointerUp={onCanvasPointerUp}
        onPointerLeave={onCanvasPointerLeave}
      >
        {nodes.length === 0 ? (
          <div className="topo-canvas-empty">
            <p>No components yet.</p>
            <button type="button" className="btn-outline btn-compact" onClick={addNode}>+ Add your first component</button>
          </div>
        ) : (
          <div className="topo-canvas-surface" style={{ width: contentW, height: contentH }}>
            {/* Edges + the in-progress link line */}
            <svg className="topo-canvas-edges" width={contentW} height={contentH}>
              <defs>
                <marker id="topo-edit-arrow" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto">
                  <polygon points="0 0, 9 3.5, 0 7" fill="#64748b" />
                </marker>
                <marker id="topo-edit-arrow-ext" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto">
                  <polygon points="0 0, 9 3.5, 0 7" fill="#f5b945" />
                </marker>
              </defs>
              {edges.map((edge, idx) => {
                const s = nodeById[edge.sourceTempId];
                const t = nodeById[edge.targetTempId];
                if (!s || !t || typeof s.x !== "number" || typeof t.x !== "number") return null;
                const { p1, p2 } = edgeEndpoints(s, t);
                const isExternal = edge.scope === "external";
                const stroke = edgeStroke(edge.scope);
                return (
                  <g key={idx} className="topo-canvas-edge" onPointerDown={(e) => e.stopPropagation()}>
                    <path
                      d={`M ${p1.x},${p1.y} L ${p2.x},${p2.y}`}
                      fill="none" stroke={stroke} strokeWidth={2}
                      strokeDasharray={isExternal ? "7 4" : undefined}
                      markerEnd={`url(#${isExternal ? "topo-edit-arrow-ext" : "topo-edit-arrow"})`}
                    />
                    {/* fat invisible hit-line to make the connection easy to click */}
                    <path d={`M ${p1.x},${p1.y} L ${p2.x},${p2.y}`}
                      fill="none" stroke="transparent" strokeWidth={16}
                      style={{ cursor: "pointer" }}
                      onClick={() => setEdgePopup({ index: idx, x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2 })}>
                      <title>Edit connection</title>
                    </path>
                  </g>
                );
              })}
              {linking && nodeById[linking.fromTempId] && (
                <path
                  d={`M ${handlePos(nodeById[linking.fromTempId]).x},${handlePos(nodeById[linking.fromTempId]).y} L ${linking.x},${linking.y}`}
                  fill="none" stroke="#38bdf8" strokeWidth={2} strokeDasharray="5 4"
                />
              )}
            </svg>

            {/* Connection labels (protocol + scope) — click to edit */}
            {edges.map((edge, idx) => {
              const s = nodeById[edge.sourceTempId];
              const t = nodeById[edge.targetTempId];
              if (!s || !t || typeof s.x !== "number" || typeof t.x !== "number") return null;
              const { p1, p2 } = edgeEndpoints(s, t);
              const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
              const isExternal = edge.scope === "external";
              return (
                <button
                  key={`elabel-${idx}`}
                  type="button"
                  className={`topo-edge-label${isExternal ? " is-external" : ""}`}
                  style={{ left: mx, top: my }}
                  onPointerDown={(e) => e.stopPropagation()}
                  onClick={() => setEdgePopup({ index: idx, x: mx, y: my })}
                  title={edge.description ? edge.description : "Edit connection"}
                >
                  <span className="topo-edge-proto">{edge.protocol || DEFAULT_PROTOCOL}</span>
                  <span className="topo-edge-scope">{isExternal ? "ext" : "int"}</span>
                  {edge.description ? <span className="topo-edge-hasdesc" aria-hidden="true">•</span> : null}
                </button>
              );
            })}

            {/* Node boxes */}
            {nodes.map((node) => {
              if (typeof node.x !== "number") return null;
              const isLinkTarget = linking && linking.fromTempId !== node.tempId;
              const isResource = Boolean(node.linkedDeployment);
              const isComponent = !isResource && Boolean(node.componentId);
              const p = posOf(node);
              const isDragging = drag && drag.tempId === node.tempId;
              return (
                <div
                  key={node.tempId}
                  className={`topo-canvas-node${linking ? " is-linkable" : ""}${isResource ? " is-resource" : ""}${isComponent ? " is-component" : ""}${isDragging ? " is-dragging" : ""}`}
                  style={{ left: p.x, top: p.y, width: E_NODE_W, height: E_NODE_H }}
                  onPointerEnter={() => { if (isLinkTarget) linkTargetRef.current = node.tempId; }}
                  onPointerLeave={() => { if (linkTargetRef.current === node.tempId) linkTargetRef.current = null; }}
                  onPointerUp={() => { if (linking && isLinkTarget) { linkTargetRef.current = node.tempId; } }}
                >
                  <div className="topo-canvas-node-header" onPointerDown={(e) => onNodePointerDown(e, node)}>
                    <span className="topo-grip">⠿</span>
                    {isResource ? (
                      <span className={`topo-node-kind topo-node-kind--${(node.type || "deployment").toLowerCase()}`}>
                        {KIND_LABEL[(node.type || "deployment").toLowerCase()] || "Deployment"}
                      </span>
                    ) : isComponent ? (
                      <>
                        <span className="topo-node-kind topo-node-kind--component">{node.type || "Component"}</span>
                        {node.componentStatus ? (
                          <span className={`topo-node-status topo-node-status--${node.componentStatus}`}
                            title={`Component health: ${node.componentStatus}`} />
                        ) : null}
                        <button type="button" className="topo-node-del" onPointerDown={(e) => e.stopPropagation()} onClick={() => removeNode(node.tempId)} title="Remove component">✕</button>
                      </>
                    ) : (
                      <button type="button" className="topo-node-del" onPointerDown={(e) => e.stopPropagation()} onClick={() => removeNode(node.tempId)} title="Remove component">✕</button>
                    )}
                  </div>
                  {isResource || isComponent ? (
                    <>
                      <div className="topo-canvas-resource-name" title={node.name}>{node.name}</div>
                      <div className="topo-canvas-resource-meta" title={isComponent ? (node.type || "") : (node.linkedNamespace || "")}>
                        {isComponent ? (node.description || node.type || "Component") : (node.linkedNamespace || "—")}
                      </div>
                    </>
                  ) : (
                    <>
                      <input
                        className="topo-canvas-input topo-canvas-input--name"
                        value={node.name}
                        onChange={(e) => updateNode(node.tempId, "name", e.target.value)}
                        onPointerDown={(e) => e.stopPropagation()}
                        placeholder="Name *"
                        maxLength={120}
                      />
                      <input
                        className="topo-canvas-input"
                        value={node.type || ""}
                        onChange={(e) => updateNode(node.tempId, "type", e.target.value)}
                        onPointerDown={(e) => e.stopPropagation()}
                        placeholder="Type (optional)"
                        maxLength={80}
                      />
                    </>
                  )}
                  <div
                    className="topo-canvas-handle"
                    title="Drag to connect"
                    onPointerDown={(e) => onHandlePointerDown(e, node)}
                  />
                </div>
              );
            })}

            {/* Connection editor popup */}
            {popupEdge ? (
              <div
                className="topo-edge-popup"
                style={{ left: edgePopup.x, top: edgePopup.y }}
                onPointerDown={(e) => e.stopPropagation()}
              >
                <div className="topo-edge-popup-title">Connection</div>
                <label className="topo-edge-popup-field">
                  <span>Protocol</span>
                  <select
                    value={popupEdge.protocol || DEFAULT_PROTOCOL}
                    onChange={(e) => updateEdge(edgePopup.index, { protocol: e.target.value })}
                  >
                    {EDGE_PROTOCOLS.map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                </label>
                <div className="topo-edge-popup-field">
                  <span>Scope</span>
                  <div className="topo-scope-toggle">
                    {["internal", "external"].map((sc) => (
                      <button
                        key={sc}
                        type="button"
                        className={(popupEdge.scope || DEFAULT_SCOPE) === sc ? "is-active" : ""}
                        onClick={() => updateEdge(edgePopup.index, { scope: sc })}
                      >
                        {sc === "internal" ? "Internal" : "External"}
                      </button>
                    ))}
                  </div>
                </div>
                <label className="topo-edge-popup-field">
                  <span>Description</span>
                  <textarea
                    className="topo-edge-popup-desc"
                    value={popupEdge.description || ""}
                    onChange={(e) => updateEdge(edgePopup.index, { description: e.target.value })}
                    placeholder="IPs, ports, notes…"
                    rows={2}
                    maxLength={1000}
                  />
                </label>
                <div className="topo-edge-popup-actions">
                  <button type="button" className="btn-ghost danger btn-compact" onClick={() => removeEdge(edgePopup.index)}>
                    Remove
                  </button>
                  <button type="button" className="btn-outline btn-compact" onClick={() => setEdgePopup(null)}>
                    Done
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Service modal ────────────────────────────────────────────────────────────

function initTopologyFromService(service) {
  if (!service?.topology) return { nodes: [], edges: [] };
  const nodes = (service.topology.nodes || []).map((n) => ({
    tempId: `node-${n.id}`,
    name: n.name,
    type: n.type || "",
    description: n.description || "",
    linkedClusterId: n.linkedClusterId || null,
    linkedNamespace: n.linkedNamespace || null,
    linkedDeployment: n.linkedDeployment || null,
    componentId: n.componentId || null,
    componentStatus: n.componentStatus || (n.component ? n.component.lastStatus : null),
    x: typeof n.positionX === "number" ? n.positionX : null,
    y: typeof n.positionY === "number" ? n.positionY : null,
  }));
  const edges = (service.topology.edges || []).map((e) => ({
    sourceTempId: `node-${e.sourceNodeId}`,
    targetTempId: `node-${e.targetNodeId}`,
    protocol: e.protocol || "",
    scope: e.scope || "",
    description: e.description || "",
  }));
  return { nodes, edges };
}

// ─── Linked-resource ↔ topology node sync ────────────────────────────────────
// Each linked resource (deployment/pod) is mirrored as a topology component.
// We key the mirror node off the resource identity (kind/cluster/ns/name), which
// is persisted via the node's linked* fields so the link survives reload.

const RES_SEP = "::";

function resourceKey(dep) {
  const kind = dep.kind || "deployment";
  return [kind, dep.clusterId, dep.namespace, dep.deploymentName].join(RES_SEP);
}

function nodeResourceKey(node) {
  if (!node.linkedDeployment || !node.linkedClusterId) return null;
  const raw = (node.type || "deployment").toLowerCase();
  const kind = WORKLOAD_KINDS.includes(raw) ? raw : "deployment";
  return [kind, node.linkedClusterId, node.linkedNamespace, node.linkedDeployment].join(RES_SEP);
}

// Reconcile topology nodes with the set of linked resources: add a component for
// each new resource, drop components whose resource was unlinked, and keep any
// manually-added (non-resource) components untouched.
function syncTopologyWithResources(topology, deployments) {
  const nodes = topology.nodes || [];
  const edges = topology.edges || [];
  const wantKeys = new Set(deployments.map(resourceKey));

  // Keep manual components and resource components that are still linked.
  const kept = nodes.filter((n) => {
    const k = nodeResourceKey(n);
    return k === null || wantKeys.has(k);
  });
  const haveKeys = new Set(kept.map(nodeResourceKey).filter(Boolean));

  // Add a component for each newly linked resource.
  const additions = [];
  deployments.forEach((dep) => {
    const k = resourceKey(dep);
    if (haveKeys.has(k)) return;
    haveKeys.add(k);
    const idx = kept.length + additions.length;
    additions.push({
      tempId: newTempId(),
      name: dep.deploymentName,
      type: dep.kind || "deployment",
      description: "",
      linkedClusterId: dep.clusterId,
      linkedNamespace: dep.namespace,
      linkedDeployment: dep.deploymentName,
      x: 40 + (idx % 4) * (E_NODE_W + 40),
      y: 40 + Math.floor(idx / 4) * (E_NODE_H + 48),
    });
  });

  const nextNodes = [...kept, ...additions];
  const keptIds = new Set(nextNodes.map((n) => n.tempId));
  const nextEdges = edges.filter((e) => keptIds.has(e.sourceTempId) && keptIds.has(e.targetTempId));
  return { nodes: nextNodes, edges: nextEdges };
}

function ServiceModal({ service, onClose, onSave, saving, error, clusters = [] }) {
  const isEdit = Boolean(service?.id);
  const [name, setName] = useState(service?.name || "");
  const [description, setDescription] = useState(service?.description || "");
  const [deployments, setDeployments] = useState(service?.deployments || []);
  // Mirror linked resources into the topology canvas on open so existing
  // services pick up a component per linked deployment/pod.
  const [topology, setTopology] = useState(() =>
    syncTopologyWithResources(initTopologyFromService(service), service?.deployments || [])
  );
  // Predefined components available to drop into the topology.
  const [components, setComponents] = useState([]);
  useEffect(() => {
    listComponents().then((r) => setComponents(r.items || [])).catch(() => setComponents([]));
  }, []);

  // Adding/removing a linked resource adds/removes its mirrored component.
  const handleDeploymentsChange = (next) => {
    setDeployments(next);
    setTopology((topo) => syncTopologyWithResources(topo, next));
  };

  const handleSubmit = () => {
    if (!name.trim()) return;
    onSave({
      name: name.trim(),
      description: description.trim(),
      deployments,
      topology: {
        nodes: topology.nodes
          .filter((n) => n.name.trim())
          .map((n) => ({
            tempId: n.tempId,
            name: n.name.trim(),
            type: n.type.trim() || undefined,
            description: n.description.trim() || undefined,
            linkedClusterId: n.linkedClusterId || undefined,
            linkedNamespace: n.linkedNamespace || undefined,
            linkedDeployment: n.linkedDeployment || undefined,
            componentId: n.componentId || undefined,
            positionX: typeof n.x === "number" ? Math.round(n.x) : undefined,
            positionY: typeof n.y === "number" ? Math.round(n.y) : undefined,
          })),
        edges: topology.edges
          .filter((e) => e.sourceTempId && e.targetTempId && e.sourceTempId !== e.targetTempId)
          .map((e) => ({
            sourceTempId: e.sourceTempId,
            targetTempId: e.targetTempId,
            protocol: e.protocol || undefined,
            scope: e.scope || undefined,
            description: e.description || undefined,
          })),
      },
    });
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>{isEdit ? "Edit Application Service" : "New Application Service"}</h3>
          <p className="muted">
            {isEdit
              ? "Update service details, topology, and linked deployments."
              : "Define a logical service with its topology and linked deployments."}
          </p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <section className="form-section">
          <h4>Service details</h4>
          <div className="form-grid">
            <label className="form-grid__full">
              Name *
              <input value={name} onChange={(e) => setName(e.target.value)}
                maxLength={120} placeholder="e.g. Billing Service" />
            </label>
            <label className="form-grid__full">
              Description
              <textarea value={description} onChange={(e) => setDescription(e.target.value)}
                rows={2} style={{ resize: "vertical" }} placeholder="Optional description" />
            </label>
          </div>
        </section>

        <section className="form-section">
          <h4>Service Topology</h4>
          <p className="muted" style={{ fontSize: "0.8125rem", marginBottom: "0.85rem" }}>
            Define the components and connections that make up this service's architecture.
          </p>
          <TopologyEditor topology={topology} onChange={setTopology} components={components} />
        </section>

        <section className="form-section">
          <h4>Linked resources</h4>
          <DeploymentPicker deployments={deployments} onChange={handleDeploymentsChange} canEdit clusters={clusters} />
        </section>

        <section className="form-section">
          <h4>Disaster Recovery <span className="muted" style={{ fontWeight: 400 }}>(optional)</span></h4>
          <p className="muted" style={{ fontSize: "0.8125rem", marginBottom: "0.85rem" }}>
            Manually link each component to its DR counterpart. The DR resource can live on a
            different cluster/namespace and have a different name.
          </p>
          {deployments.length === 0 ? (
            <p className="muted" style={{ fontSize: "0.85rem" }}>Link resources above first, then map them to DR here.</p>
          ) : (
            <div className="dr-link-list">
              {deployments.map((dep, idx) => (
                <div key={`${dep.kind || "deployment"}/${dep.clusterId}/${dep.namespace}/${dep.deploymentName}`} className="dr-link-row">
                  <div className="dr-link-primary">
                    <span className="form-label" style={{ fontSize: "0.72rem" }}>Primary</span>
                    <DeploymentRow dep={dep} clusterName={(clusters.find((c) => c.id === dep.clusterId) || {}).name || dep.clusterId} />
                  </div>
                  <span className="dr-link-arrow" aria-hidden="true">→</span>
                  <div className="dr-link-dr">
                    <span className="form-label" style={{ fontSize: "0.72rem" }}>Disaster recovery</span>
                    <DRLinker
                      dr={dep.dr}
                      clusters={clusters}
                      onChange={(drObj) =>
                        setDeployments((prev) => prev.map((d, i) => (i === idx ? { ...d, dr: drObj || undefined } : d)))
                      }
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>Cancel</button>
          <button type="button" className="primary" onClick={handleSubmit} disabled={saving || !name.trim()}>
            {saving ? "Saving…" : isEdit ? "Save changes" : "Create service"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Service detail panel ────────────────────────────────────────────────────

function tabStyle(active) {
  return {
    background: "none",
    border: "none",
    padding: "0.5rem 0.85rem",
    cursor: "pointer",
    fontWeight: 600,
    fontSize: "0.875rem",
    color: active ? "var(--accent, #38bdf8)" : "var(--text-muted, #94a3b8)",
    borderBottom: active ? "2px solid var(--accent, #38bdf8)" : "2px solid transparent",
    marginBottom: "-1px",
  };
}

function DRSheet({ service, clusterNameById }) {
  const drDeployments = (service.deployments || []).filter((d) => d.dr);
  const noDr = (service.deployments || []).filter((d) => !d.dr);

  if (drDeployments.length === 0) {
    return (
      <div className="dr-sheet">
        <EmptyState
          message="No disaster recovery configured"
          hint="Edit this service to link its components to their DR counterparts."
          variant="applications"
        />
      </div>
    );
  }

  return (
    <div className="dr-sheet">
      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1rem" }}>
        <span className="form-label" style={{ margin: 0 }}>Overall DR status</span>
        <DrBadge hasDr={service.hasDr} drHealth={service.drHealth} />
      </div>

      <div className="table-shell">
        <div className="table-scroll-region">
          <table>
            <thead>
              <tr>
                <th>Primary component</th>
                <th>DR counterpart</th>
                <th>DR status</th>
              </tr>
            </thead>
            <tbody>
              {drDeployments.map((dep) => (
                <tr key={`${dep.clusterId}/${dep.namespace}/${dep.deploymentName}`}>
                  <td>
                    <DeploymentRow dep={dep} clusterName={clusterNameById?.[dep.clusterId]} />
                  </td>
                  <td>
                    <DeploymentRow
                      dep={{ clusterId: dep.dr.clusterId, namespace: dep.dr.namespace, deploymentName: dep.dr.deploymentName, kind: dep.dr.kind }}
                      clusterName={clusterNameById?.[dep.dr.clusterId]}
                    />
                  </td>
                  <td><HealthBadge health={dep.drStatus || "unknown"} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {noDr.length > 0 && (
        <div style={{ marginTop: "1rem" }}>
          <p className="form-label" style={{ marginBottom: "0.5rem" }}>Components without DR</p>
          {noDr.map((dep) => (
            <div key={`${dep.clusterId}/${dep.namespace}/${dep.deploymentName}`}
              style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <DeploymentRow dep={dep} clusterName={clusterNameById?.[dep.clusterId]} />
              <span className="status-badge status-badge--pending">No DR</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ServiceDetailPanel({ service, clusterNameById, onEdit, onDelete, canEdit, canDelete, onClose }) {
  const topo = service.topology || { nodes: [], edges: [] };
  const [tab, setTab] = useState("overview");

  // Close on Escape.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose?.(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide service-detail-modal" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="service-detail-modal__head">
          <div>
            <h3 style={{ margin: 0 }}>{service.name}</h3>
            {service.description && (
              <p className="muted" style={{ marginTop: "0.25rem", fontSize: "0.875rem" }}>{service.description}</p>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
            <HealthBadge health={service.health} />
            <button type="button" className="btn-ghost modal-close" onClick={onClose} aria-label="Close">✕</button>
          </div>
        </div>

        {/* Sheet tabs: main service view ↔ DR status */}
        <div className="service-detail-tabs" role="tablist"
          style={{ display: "flex", gap: "0.25rem", borderBottom: "1px solid var(--border, #1e293b)", margin: "0.75rem 0 1rem" }}>
          <button type="button" role="tab" aria-selected={tab === "overview"}
            className={`tab-btn${tab === "overview" ? " tab-btn--active" : ""}`}
            onClick={() => setTab("overview")}
            style={tabStyle(tab === "overview")}>
            Overview
          </button>
          <button type="button" role="tab" aria-selected={tab === "dr"}
            className={`tab-btn${tab === "dr" ? " tab-btn--active" : ""}`}
            onClick={() => setTab("dr")}
            style={tabStyle(tab === "dr")}>
            DR Status
            <span style={{ marginLeft: "0.5rem", verticalAlign: "middle" }}>
              <DrBadge hasDr={service.hasDr} drHealth={service.drHealth} />
            </span>
          </button>
        </div>

        {tab === "overview" ? (
          <>
            <div style={{ display: "flex", gap: "1.5rem", fontSize: "0.85rem", color: "var(--text-muted, #888)", margin: "0 0 1rem" }}>
              <span>{service.deploymentCount ?? 0} deployment{service.deploymentCount !== 1 ? "s" : ""}</span>
              {topo.nodes.length > 0 && <span>{topo.nodes.length} component{topo.nodes.length !== 1 ? "s" : ""}</span>}
              {service.createdAt && <span>Created {new Date(service.createdAt).toLocaleDateString()}</span>}
            </div>

            {topo.nodes.length > 0 && (
              <div style={{ marginBottom: "1.25rem" }}>
                <p className="form-label" style={{ marginBottom: "0.6rem" }}>Service Topology</p>
                <TopologyViewer nodes={topo.nodes} edges={topo.edges} />
              </div>
            )}

            {(service.deployments || []).length > 0 && (
              <div style={{ marginBottom: "1.25rem" }}>
                <p className="form-label" style={{ marginBottom: "0.5rem" }}>Linked resources</p>
                {service.deployments.map((dep) => (
                  <DeploymentRow
                    key={`${dep.clusterId}/${dep.namespace}/${dep.deploymentName}`}
                    dep={dep}
                    clusterName={clusterNameById?.[dep.clusterId]}
                  />
                ))}
              </div>
            )}
          </>
        ) : (
          <DRSheet service={service} clusterNameById={clusterNameById} />
        )}

        <div className="modal-actions">
          {canEdit && <button className="btn-outline btn-compact" onClick={onEdit}>Edit</button>}
          {canDelete && <button className="btn-outline btn-compact danger" onClick={onDelete}>Delete</button>}
          <button className="btn-outline btn-compact" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ApplicationServicesPage({ clusters: clustersProp = [] }) {
  const { hasPermission } = useAuth();
  const canView = hasPermission("app_services:view");
  const canCreate = hasPermission("app_services:create");
  const canUpdate = hasPermission("app_services:update");
  const canDelete = hasPermission("app_services:delete");

  const [services, setServices] = useState([]);
  const clusters = clustersProp;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [healthFilter, setHealthFilter] = useState("all");
  const [selectedId, setSelectedId] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingService, setEditingService] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [deleting, setDeleting] = useState(false);

  const selectedService = services.find((s) => s.id === selectedId) || null;

  const loadServices = async () => {
    setLoading(true);
    setError("");
    try {
      const svcRes = await listApplicationServices();
      setServices(svcRes.items || []);
    } catch (err) {
      setError(err.message || "Failed to load application services.");
    } finally { setLoading(false); }
  };

  useEffect(() => { loadServices(); }, []);

  const openCreate = () => { setEditingService(null); setSaveError(""); setModalOpen(true); };
  const openEdit = (svc) => { setEditingService(svc); setSaveError(""); setModalOpen(true); };
  const closeModal = () => { setModalOpen(false); setEditingService(null); setSaveError(""); };

  const handleSave = async (payload) => {
    setSaving(true);
    setSaveError("");
    try {
      if (editingService?.id) {
        const updated = await updateApplicationService(editingService.id, payload);
        setServices((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
        setSelectedId(updated.id);
      } else {
        const created = await createApplicationService(payload);
        setServices((prev) => [...prev, created]);
        setSelectedId(created.id);
      }
      closeModal();
    } catch (err) {
      setSaveError(err.message || "Save failed.");
    } finally { setSaving(false); }
  };

  const handleDelete = async (svc) => {
    if (!window.confirm(`Delete "${svc.name}"? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      await deleteApplicationService(svc.id);
      setServices((prev) => prev.filter((s) => s.id !== svc.id));
      if (selectedId === svc.id) setSelectedId(null);
    } catch (err) {
      setError(err.message || "Delete failed.");
    } finally { setDeleting(false); }
  };

  if (!canView) return <AccessDeniedPage />;

  const filtered = services.filter((svc) => {
    if (healthFilter !== "all" && svc.health !== healthFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return svc.name.toLowerCase().includes(q) || (svc.description || "").toLowerCase().includes(q);
    }
    return true;
  });

  return (
    <div className="ops-page">
      <PageTitle
        title="Application Services"
        subtitle="Logical service groupings with topology and deployment mappings."
        actionLabel={canCreate ? "New service" : undefined}
        onAction={canCreate ? openCreate : undefined}
      />

      {error && <p className="banner-message error" style={{ marginBottom: "1rem" }}>{error}</p>}

      <div className="user-filters" style={{ marginBottom: "1rem" }}>
        <input type="search" className="form-input" placeholder="Search by name or description…"
          value={search} onChange={(e) => setSearch(e.target.value)} style={{ maxWidth: 280 }} />
        <SearchableSelect className="form-select" value={healthFilter} onChange={(e) => setHealthFilter(e.target.value)}>
          <option value="all">All health states</option>
          <option value="healthy">Healthy</option>
          <option value="warning">Warning</option>
          <option value="critical">Critical</option>
          <option value="unknown">Unknown</option>
        </SearchableSelect>
      </div>

      <div>
        <div>
          {loading ? (
            <LoadingState label="Loading application services…" />
          ) : filtered.length === 0 ? (
            <EmptyState
              message="No application services found."
              hint={services.length > 0 ? "Try adjusting the search or filter." : "Create your first service to get started."}
            />
          ) : (
            <div className="table-shell">
              <div className="table-scroll-region">
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Health</th>
                      <th>DR Status</th>
                      <th>Topology</th>
                      <th>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((svc) => {
                      const nodeCount = svc.topology?.nodes?.length ?? 0;
                      return (
                        <tr key={svc.id}
                          className={selectedId === svc.id ? "table-row--selected" : ""}
                          style={{ cursor: "pointer" }}
                          onClick={() => setSelectedId(svc.id === selectedId ? null : svc.id)}>
                          <td>
                            <strong>{svc.name}</strong>
                            {svc.description && (
                              <div className="muted" style={{ fontSize: "0.8rem" }}>{svc.description}</div>
                            )}
                          </td>
                          <td><HealthBadge health={svc.health} /></td>
                          <td><DrBadge hasDr={svc.hasDr} drHealth={svc.drHealth} /></td>
                          <td className="muted" style={{ fontSize: "0.8rem" }}>
                            {nodeCount > 0 ? `${nodeCount} component${nodeCount !== 1 ? "s" : ""}` : "—"}
                          </td>
                          <td className="muted" style={{ fontSize: "0.8rem" }}>
                            {svc.updatedAt ? new Date(svc.updatedAt).toLocaleDateString() : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

      </div>

      {selectedService && (
        <ServiceDetailPanel
          service={selectedService}
          clusterNameById={Object.fromEntries(clusters.map((c) => [c.id, c.name || c.id]))}
          onEdit={() => { const svc = selectedService; setSelectedId(null); openEdit(svc); }}
          onDelete={() => handleDelete(selectedService)}
          onClose={() => setSelectedId(null)}
          canEdit={canUpdate}
          canDelete={canDelete && !deleting}
        />
      )}

      {modalOpen && (
        <ServiceModal
          service={editingService}
          onClose={closeModal}
          onSave={handleSave}
          saving={saving}
          error={saveError}
          clusters={clusters}
        />
      )}
    </div>
  );
}
