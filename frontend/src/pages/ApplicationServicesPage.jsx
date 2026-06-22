import { useEffect, useMemo, useRef, useState } from "react";
import {
  createApplicationService,
  deleteApplicationService,
  listApplicationServices,
  listNamespacesByCluster,
  listPickerDeployments,
  listPickerPods,
  updateApplicationService,
} from "../api";
import { useAuth } from "../context/AuthContext";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import PageTitle from "../components/common/PageTitle.jsx";

// ─── Utilities ──────────────────────────────────────────────────────────────

function SearchableSelect({ options, value, onChange, placeholder = "Select…", disabled = false }) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [dropPos, setDropPos] = useState({ top: 0, left: 0, width: 0 });
  const triggerRef = useRef(null);
  const dropRef = useRef(null);

  useEffect(() => {
    if (!open) { setSearch(""); return; }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) setDropPos({ top: rect.bottom + window.scrollY + 3, left: rect.left + window.scrollX, width: rect.width });
  }, [open]);

  useEffect(() => {
    const handler = (e) => {
      if (
        triggerRef.current && !triggerRef.current.contains(e.target) &&
        dropRef.current && !dropRef.current.contains(e.target)
      ) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const filtered = options.filter((o) =>
    (o.label ?? o).toString().toLowerCase().includes(search.toLowerCase())
  );
  const selectedLabel = value
    ? (options.find((o) => (o.value ?? o) === value)?.label ?? value)
    : placeholder;

  return (
    <div className="ss-wrap">
      <button
        ref={triggerRef}
        type="button"
        className={`ss-trigger${open ? " ss-trigger--open" : ""}${disabled ? " ss-trigger--disabled" : ""}`}
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled}
      >
        <span className={value ? "" : "ss-placeholder"}>{selectedLabel}</span>
        <span className="ss-arrow">▾</span>
      </button>
      {open && (
        <div ref={dropRef} className="ss-dropdown"
          style={{ position: "fixed", top: dropPos.top, left: dropPos.left, minWidth: dropPos.width }}>
          {options.length > 5 && (
            <div className="ss-search-wrap">
              <input className="ss-search" autoFocus placeholder="Search…" value={search}
                onChange={(e) => setSearch(e.target.value)} onMouseDown={(e) => e.stopPropagation()} />
            </div>
          )}
          <div className="ss-list">
            {filtered.length === 0 ? <div className="ss-empty">No results</div> : (
              filtered.map((opt) => {
                const v = opt.value ?? opt;
                const l = opt.label ?? opt;
                return (
                  <div key={v} className={`ss-option${v === value ? " ss-option--selected" : ""}`}
                    onMouseDown={() => { onChange(v); setOpen(false); }}>
                    {l}
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Health badge ────────────────────────────────────────────────────────────

const HEALTH_BADGE = { healthy: "pass", warning: "warning", critical: "fail", unknown: "pending" };

function HealthBadge({ health }) {
  const variant = HEALTH_BADGE[health] || "pending";
  return <span className={`status-badge status-badge--${variant}`}>{health || "unknown"}</span>;
}

// ─── Deployment picker components ────────────────────────────────────────────

function DeploymentPickerList({ deployments: items, linked, clusterId, namespace, kind, onAdd }) {
  const [search, setSearch] = useState("");
  const filtered = items.filter((dep) => {
    const name = typeof dep === "string" ? dep : dep.name;
    return name.toLowerCase().includes(search.toLowerCase());
  });
  return (
    <div className="dep-pick-list-wrap">
      {items.length > 5 && (
        <input className="ss-search dep-pick-search" placeholder={`Search ${kind}s…`}
          value={search} onChange={(e) => setSearch(e.target.value)} />
      )}
      <div className="dep-pick-scroll">
        {filtered.length === 0 ? (
          <p className="muted" style={{ fontSize: "0.85rem", padding: "0.35rem 0" }}>No matches.</p>
        ) : (
          filtered.map((dep) => {
            const depName = typeof dep === "string" ? dep : dep.name;
            const alreadyAdded = linked.some(
              (d) => d.clusterId === clusterId && d.namespace === namespace && d.deploymentName === depName && (d.kind || "deployment") === kind
            );
            return (
              <div key={depName} className="dep-picker-option">
                <span>{depName}</span>
                <button type="button" className="btn-outline btn-compact"
                  onClick={() => onAdd(depName)} disabled={alreadyAdded}>
                  {alreadyAdded ? "Added" : "Add"}
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function DeploymentRow({ dep, clusterName, onRemove }) {
  const kind = dep.kind || "deployment";
  return (
    <div className="dep-picker-row">
      <span className={`dep-picker-kind dep-picker-kind--${kind}`}>{kind}</span>
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
      const fn = kind === "pod" ? listPickerPods : listPickerDeployments;
      const res = await fn(cluster, namespace);
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
          <div className="dep-picker-kind-toggle" style={{ display: "flex", gap: "0.375rem", marginBottom: "0.5rem" }}>
            {["deployment", "pod"].map((k) => (
              <button key={k} type="button"
                className={`btn-outline btn-compact${pickerKind === k ? " dep-picker-kind-active" : ""}`}
                onClick={() => handleKindChange(k)}
                style={{ textTransform: "capitalize" }}>
                {k}
              </button>
            ))}
          </div>
          <div className="dep-picker-controls">
            <SearchableSelect
              options={clusters.map((c) => ({ value: c.id, label: c.name || c.id }))}
              value={pickerCluster}
              onChange={handleClusterChange}
              placeholder="Select cluster…"
            />
            <SearchableSelect
              options={nsOptions.map((ns) => ({ value: ns, label: ns }))}
              value={pickerNamespace}
              onChange={handleNamespaceChange}
              placeholder={nsLoading ? "Loading…" : "Select namespace…"}
              disabled={!pickerCluster || nsLoading}
            />
          </div>
          {pickerError && <p className="banner-message error" style={{ marginTop: "0.5rem" }}>{pickerError}</p>}
          {itemsLoading && <p className="muted" style={{ fontSize: "0.85rem" }}>Loading {pickerKind}s…</p>}
          {pickerItems.length > 0 && (
            <DeploymentPickerList deployments={pickerItems} linked={deployments}
              clusterId={pickerCluster} namespace={pickerNamespace} kind={pickerKind} onAdd={addDeployment} />
          )}
          {pickerNamespace && !itemsLoading && pickerItems.length === 0 && (
            <p className="muted" style={{ fontSize: "0.85rem" }}>No {pickerKind}s found in this namespace.</p>
          )}
        </div>
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

  return (
    <div className={`topo-viewer${compact ? " topo-viewer--compact" : ""}`}>
      <svg viewBox={`0 0 ${svgW} ${svgH}`}
        style={{ display: "block", width: "100%", height: "auto" }}>
        <defs>
          <filter id="topo-shadow" x="-30%" y="-30%" width="160%" height="160%">
            <feDropShadow dx="0" dy="2" stdDeviation="4" floodColor="rgba(0,0,0,0.5)" />
          </filter>
          <marker id="topo-arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="#64748b" />
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
            if (hasBidi) {
              const sign = String(edge.sourceNodeId) < String(edge.targetNodeId) ? 1 : -1;
              const px = -ny * 16 * sign, py = nx * 16 * sign;
              const mx = (p1.x + p2.x) / 2 + px, my = (p1.y + p2.y) / 2 + py;
              d = `M ${p1.x},${p1.y} Q ${mx},${my} ${p2.x},${p2.y}`;
            } else {
              d = `M ${p1.x},${p1.y} L ${p2.x},${p2.y}`;
            }

            return (
              <path key={edge.id ?? `e${idx}`} d={d}
                fill="none" stroke="#475569" strokeWidth={1.5}
                markerEnd="url(#topo-arrow)" />
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

            return (
              <g key={node.id} title={node.description || node.name}>
                <rect x={pos.x} y={pos.y} width={NODE_W} height={NODE_H}
                  rx={8} ry={8}
                  fill="#1e293b" stroke="#334155" strokeWidth={1.5}
                  filter="url(#topo-shadow)" />
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

function TopologyEditor({ topology, onChange }) {
  const { nodes, edges } = topology;
  const canvasRef = useRef(null);
  const dragRef = useRef(null);          // { tempId, dx, dy } while dragging a node
  const [linking, setLinking] = useState(null); // { fromTempId, x, y } while drawing an edge
  const linkTargetRef = useRef(null);    // tempId currently hovered while linking

  const nodeById = useMemo(() => {
    const m = {};
    nodes.forEach((n) => { m[n.tempId] = n; });
    return m;
  }, [nodes]);

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

  const canvasPoint = (evt) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: evt.clientX - rect.left + canvasRef.current.scrollLeft,
      y: evt.clientY - rect.top + canvasRef.current.scrollTop,
    };
  };

  const addNode = () => {
    // Drop the new box into a free area near the top-left, staggered by count.
    const i = nodes.length;
    const x = 40 + (i % 4) * (E_NODE_W + 40);
    const y = 40 + Math.floor(i / 4) * (E_NODE_H + 48);
    onChange({
      nodes: [...nodes, { tempId: newTempId(), name: "", type: "", description: "", x, y }],
      edges,
    });
  };

  const updateNode = (tempId, field, value) => {
    onChange({ nodes: nodes.map((n) => (n.tempId === tempId ? { ...n, [field]: value } : n)), edges });
  };

  const removeNode = (tempId) => {
    onChange({
      nodes: nodes.filter((n) => n.tempId !== tempId),
      edges: edges.filter((e) => e.sourceTempId !== tempId && e.targetTempId !== tempId),
    });
  };

  const removeEdge = (idx) => {
    onChange({ nodes, edges: edges.filter((_, i) => i !== idx) });
  };

  // ── Node dragging ──
  const onNodePointerDown = (evt, node) => {
    if (evt.button !== 0) return;
    evt.stopPropagation();
    const p = canvasPoint(evt);
    dragRef.current = { tempId: node.tempId, dx: p.x - node.x, dy: p.y - node.y, moved: false };
  };

  // ── Edge linking ──
  const onHandlePointerDown = (evt, node) => {
    if (evt.button !== 0) return;
    evt.stopPropagation();
    const p = canvasPoint(evt);
    setLinking({ fromTempId: node.tempId, x: p.x, y: p.y });
    linkTargetRef.current = null;
  };

  const onCanvasPointerMove = (evt) => {
    const p = canvasPoint(evt);
    if (dragRef.current) {
      const { tempId, dx, dy } = dragRef.current;
      dragRef.current.moved = true;
      const nx = Math.max(0, p.x - dx);
      const ny = Math.max(0, p.y - dy);
      onChange({ nodes: nodes.map((n) => (n.tempId === tempId ? { ...n, x: nx, y: ny } : n)), edges });
    } else if (linking) {
      setLinking((l) => (l ? { ...l, x: p.x, y: p.y } : l));
    }
  };

  const finishLinking = () => {
    const from = linking?.fromTempId;
    const to = linkTargetRef.current;
    if (from && to && from !== to) {
      const dup = edges.some((e) => e.sourceTempId === from && e.targetTempId === to);
      if (!dup) onChange({ nodes, edges: [...edges, { sourceTempId: from, targetTempId: to }] });
    }
    setLinking(null);
    linkTargetRef.current = null;
  };

  const onCanvasPointerUp = () => {
    dragRef.current = null;
    if (linking) finishLinking();
  };

  // Geometry for rendering edges between editor boxes.
  const centerOf = (n) => ({ x: n.x + E_NODE_W / 2, y: n.y + E_NODE_H / 2 });
  const handlePos = (n) => ({ x: n.x + E_NODE_W, y: n.y + E_NODE_H / 2 });

  const placedNodes = nodes.filter((n) => typeof n.x === "number" && typeof n.y === "number");
  const contentW = placedNodes.reduce((m, n) => Math.max(m, n.x + E_NODE_W), 0) + 60;
  const contentH = placedNodes.reduce((m, n) => Math.max(m, n.y + E_NODE_H), 0) + 60;

  return (
    <div className="topo-editor">
      <div className="topo-editor-block-header">
        <span className="topo-editor-block-title">Topology canvas</span>
        <button type="button" className="btn-outline btn-compact" onClick={addNode}>+ Add component</button>
      </div>
      <p className="muted topo-canvas-hint">
        Drag a box by its header to move it. Drag from the <span className="topo-handle-legend">●</span> handle on the right
        of a box to another box to connect them. Click a connection to remove it.
      </p>

      <div
        ref={canvasRef}
        className="topo-canvas"
        style={{ height: nodes.length ? Math.max(TOPO_CANVAS_MIN_H, contentH) : TOPO_CANVAS_MIN_H }}
        onPointerMove={onCanvasPointerMove}
        onPointerUp={onCanvasPointerUp}
        onPointerLeave={onCanvasPointerUp}
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
              </defs>
              {edges.map((edge, idx) => {
                const s = nodeById[edge.sourceTempId];
                const t = nodeById[edge.targetTempId];
                if (!s || !t || typeof s.x !== "number" || typeof t.x !== "number") return null;
                const sc = centerOf(s), tc = centerOf(t);
                const dx = tc.x - sc.x, dy = tc.y - sc.y;
                const len = Math.hypot(dx, dy) || 1;
                const nx = dx / len, ny = dy / len;
                const p1 = rectExitPoint(sc.x, sc.y, nx, ny, E_NODE_W, E_NODE_H);
                const p2 = rectExitPoint(tc.x, tc.y, -nx, -ny, E_NODE_W, E_NODE_H);
                const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
                return (
                  <g key={idx} className="topo-canvas-edge" onPointerDown={(e) => e.stopPropagation()}>
                    <path
                      d={`M ${p1.x},${p1.y} L ${p2.x},${p2.y}`}
                      fill="none" stroke="#475569" strokeWidth={2}
                      markerEnd="url(#topo-edit-arrow)"
                    />
                    {/* fat invisible hit-line + delete badge */}
                    <path d={`M ${p1.x},${p1.y} L ${p2.x},${p2.y}`}
                      fill="none" stroke="transparent" strokeWidth={14}
                      style={{ cursor: "pointer" }}
                      onClick={() => removeEdge(idx)}>
                      <title>Remove connection</title>
                    </path>
                    <g className="topo-edge-delete" onClick={() => removeEdge(idx)} style={{ cursor: "pointer" }}>
                      <circle cx={mx} cy={my} r={9} />
                      <text x={mx} y={my + 0.5} textAnchor="middle" dominantBaseline="middle">✕</text>
                    </g>
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

            {/* Node boxes */}
            {nodes.map((node) => {
              if (typeof node.x !== "number") return null;
              const isLinkTarget = linking && linking.fromTempId !== node.tempId;
              return (
                <div
                  key={node.tempId}
                  className={`topo-canvas-node${linking ? " is-linkable" : ""}`}
                  style={{ left: node.x, top: node.y, width: E_NODE_W, height: E_NODE_H }}
                  onPointerEnter={() => { if (isLinkTarget) linkTargetRef.current = node.tempId; }}
                  onPointerLeave={() => { if (linkTargetRef.current === node.tempId) linkTargetRef.current = null; }}
                  onPointerUp={() => { if (linking && isLinkTarget) { linkTargetRef.current = node.tempId; } }}
                >
                  <div className="topo-canvas-node-header" onPointerDown={(e) => onNodePointerDown(e, node)}>
                    <span className="topo-grip">⠿</span>
                    <button type="button" className="topo-node-del" onClick={() => removeNode(node.tempId)} title="Remove component">✕</button>
                  </div>
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
                  <div
                    className="topo-canvas-handle"
                    title="Drag to connect"
                    onPointerDown={(e) => onHandlePointerDown(e, node)}
                  />
                </div>
              );
            })}
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
    x: typeof n.positionX === "number" ? n.positionX : null,
    y: typeof n.positionY === "number" ? n.positionY : null,
  }));
  const edges = (service.topology.edges || []).map((e) => ({
    sourceTempId: `node-${e.sourceNodeId}`,
    targetTempId: `node-${e.targetNodeId}`,
  }));
  return { nodes, edges };
}

function ServiceModal({ service, onClose, onSave, saving, error, clusters = [] }) {
  const isEdit = Boolean(service?.id);
  const [name, setName] = useState(service?.name || "");
  const [description, setDescription] = useState(service?.description || "");
  const [deployments, setDeployments] = useState(service?.deployments || []);
  const [topology, setTopology] = useState(() => initTopologyFromService(service));

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
            positionX: typeof n.x === "number" ? Math.round(n.x) : undefined,
            positionY: typeof n.y === "number" ? Math.round(n.y) : undefined,
          })),
        edges: topology.edges
          .filter((e) => e.sourceTempId && e.targetTempId && e.sourceTempId !== e.targetTempId)
          .map((e) => ({ sourceTempId: e.sourceTempId, targetTempId: e.targetTempId })),
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
          <TopologyEditor topology={topology} onChange={setTopology} />
        </section>

        <section className="form-section">
          <h4>Linked resources</h4>
          <DeploymentPicker deployments={deployments} onChange={setDeployments} canEdit clusters={clusters} />
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

function ServiceDetailPanel({ service, clusterNameById, onEdit, onDelete, canEdit, canDelete }) {
  const topo = service.topology || { nodes: [], edges: [] };

  return (
    <div className="card" style={{ padding: "1.25rem" }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "1rem", marginBottom: "1rem" }}>
        <div>
          <h3 style={{ margin: 0 }}>{service.name}</h3>
          {service.description && (
            <p className="muted" style={{ marginTop: "0.25rem", fontSize: "0.875rem" }}>{service.description}</p>
          )}
        </div>
        <HealthBadge health={service.health} />
      </div>

      <div style={{ display: "flex", gap: "1.5rem", fontSize: "0.85rem", color: "var(--color-muted, #888)", marginBottom: "1.25rem" }}>
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

      <div style={{ display: "flex", gap: "0.75rem" }}>
        {canEdit && <button className="btn-outline btn-compact" onClick={onEdit}>Edit</button>}
        {canDelete && <button className="btn-outline btn-compact danger" onClick={onDelete}>Delete</button>}
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
        <select className="form-select" value={healthFilter} onChange={(e) => setHealthFilter(e.target.value)}>
          <option value="all">All health states</option>
          <option value="healthy">Healthy</option>
          <option value="warning">Warning</option>
          <option value="critical">Critical</option>
          <option value="unknown">Unknown</option>
        </select>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: selectedService ? "1fr 1fr" : "1fr", gap: "1.25rem", alignItems: "start" }}>
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

        {selectedService && (
          <ServiceDetailPanel
            service={selectedService}
            clusterNameById={Object.fromEntries(clusters.map((c) => [c.id, c.name || c.id]))}
            onEdit={() => openEdit(selectedService)}
            onDelete={() => handleDelete(selectedService)}
            canEdit={canUpdate}
            canDelete={canDelete && !deleting}
          />
        )}
      </div>

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
