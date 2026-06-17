import { useEffect, useMemo, useRef, useState } from "react";
import {
  createApplicationService,
  deleteApplicationService,
  listApplicationServices,
  listNamespacesByCluster,
  listPickerDeployments,
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

function DeploymentPickerList({ deployments: items, linked, clusterId, namespace, onAdd }) {
  const [search, setSearch] = useState("");
  const filtered = items.filter((dep) => {
    const name = typeof dep === "string" ? dep : dep.name;
    return name.toLowerCase().includes(search.toLowerCase());
  });
  return (
    <div className="dep-pick-list-wrap">
      {items.length > 5 && (
        <input className="ss-search dep-pick-search" placeholder="Search deployments…"
          value={search} onChange={(e) => setSearch(e.target.value)} />
      )}
      <div className="dep-pick-scroll">
        {filtered.length === 0 ? (
          <p className="muted" style={{ fontSize: "0.85rem", padding: "0.35rem 0" }}>No matches.</p>
        ) : (
          filtered.map((dep) => {
            const depName = typeof dep === "string" ? dep : dep.name;
            const alreadyAdded = linked.some(
              (d) => d.clusterId === clusterId && d.namespace === namespace && d.deploymentName === depName
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
  return (
    <div className="dep-picker-row">
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
  const [pickerDeployments, setPickerDeployments] = useState([]);
  const [pickerCluster, setPickerCluster] = useState("");
  const [pickerNamespace, setPickerNamespace] = useState("");
  const [nsLoading, setNsLoading] = useState(false);
  const [depLoading, setDepLoading] = useState(false);
  const [pickerError, setPickerError] = useState("");

  const handleClusterChange = async (val) => {
    setPickerCluster(val);
    setPickerNamespace("");
    setNamespaces([]);
    setPickerDeployments([]);
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

  const handleNamespaceChange = async (val) => {
    setPickerNamespace(val);
    setPickerDeployments([]);
    setPickerError("");
    if (!val || !pickerCluster) return;
    setDepLoading(true);
    try {
      const res = await listPickerDeployments(pickerCluster, val);
      setPickerDeployments(res.items || []);
    } catch (err) {
      setPickerError(err.message || "Failed to load deployments");
    } finally { setDepLoading(false); }
  };

  const addDeployment = (depName) => {
    const entry = { clusterId: pickerCluster, namespace: pickerNamespace, deploymentName: depName };
    if (!deployments.some((d) => d.clusterId === entry.clusterId && d.namespace === entry.namespace && d.deploymentName === entry.deploymentName)) {
      onChange([...deployments, entry]);
    }
  };

  const clusterNameById = Object.fromEntries(clusters.map((c) => [c.id, c.name || c.id]));
  const nsOptions = (namespaces.map((n) => (typeof n === "string" ? n : n.name)));

  return (
    <div className="dep-picker">
      <div className="dep-picker-current">
        {deployments.length === 0 ? (
          <p className="muted" style={{ fontSize: "0.875rem" }}>None linked yet.</p>
        ) : (
          deployments.map((dep, idx) => (
            <DeploymentRow
              key={`${dep.clusterId}/${dep.namespace}/${dep.deploymentName}`}
              dep={dep}
              clusterName={clusterNameById[dep.clusterId]}
              onRemove={canEdit ? () => onChange(deployments.filter((_, i) => i !== idx)) : null}
            />
          ))
        )}
      </div>
      {canEdit && (
        <div className="dep-picker-add">
          <p className="muted" style={{ fontSize: "0.8125rem", fontWeight: 500, marginBottom: "0.4rem" }}>Add deployment</p>
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
          {depLoading && <p className="muted" style={{ fontSize: "0.85rem" }}>Loading deployments…</p>}
          {pickerDeployments.length > 0 && (
            <DeploymentPickerList deployments={pickerDeployments} linked={deployments}
              clusterId={pickerCluster} namespace={pickerNamespace} onAdd={addDeployment} />
          )}
          {pickerNamespace && !depLoading && pickerDeployments.length === 0 && (
            <p className="muted" style={{ fontSize: "0.85rem" }}>No deployments found in this namespace.</p>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Topology layout ─────────────────────────────────────────────────────────

const NODE_W = 144;
const NODE_H = 52;
const H_GAP = 56;
const V_GAP = 64;
const PAD = 24;

function computeLayout(nodes, edges) {
  if (!nodes.length) return { positions: {}, svgW: NODE_W + PAD * 2, svgH: NODE_H + PAD * 2 };

  const ids = nodes.map((n) => String(n.id));
  const out = Object.fromEntries(ids.map((id) => [id, []]));
  const inCount = Object.fromEntries(ids.map((id) => [id, 0]));

  edges.forEach((e) => {
    const s = String(e.sourceNodeId);
    const t = String(e.targetNodeId);
    if (s in out && t in inCount) {
      out[s].push(t);
      inCount[t]++;
    }
  });

  // Longest-path level assignment (cycle-safe via visited set per DFS path)
  const levels = Object.fromEntries(ids.map((id) => [id, 0]));
  const roots = ids.filter((id) => inCount[id] === 0);
  const starts = roots.length ? roots : [ids[0]];

  const assign = (id, lv, path) => {
    if (path.has(id)) return; // cycle guard
    if (levels[id] >= lv && lv > 0) {
      // re-enter only if we can push deeper
      if (levels[id] >= lv) return;
    }
    levels[id] = Math.max(levels[id], lv);
    const next = new Set([...path, id]);
    out[id].forEach((nid) => assign(nid, lv + 1, next));
  };
  starts.forEach((id) => assign(id, 0, new Set()));

  // Group by level
  const byLevel = {};
  ids.forEach((id) => {
    const lv = levels[id];
    (byLevel[lv] = byLevel[lv] || []).push(id);
  });

  const maxLv = Math.max(...Object.keys(byLevel).map(Number));
  const maxCount = Math.max(...Object.values(byLevel).map((a) => a.length));

  const totalW = maxCount * NODE_W + Math.max(0, maxCount - 1) * H_GAP;
  const totalH = (maxLv + 1) * NODE_H + maxLv * V_GAP;

  const positions = {};
  Object.entries(byLevel).forEach(([lv, lvIds]) => {
    const n = lvIds.length;
    const lvW = n * NODE_W + Math.max(0, n - 1) * H_GAP;
    const x0 = (totalW - lvW) / 2;
    lvIds.forEach((id, i) => {
      positions[id] = { x: x0 + i * (NODE_W + H_GAP), y: +lv * (NODE_H + V_GAP) };
    });
  });

  return { positions, svgW: totalW + PAD * 2, svgH: totalH + PAD * 2 };
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
      <svg width={svgW} height={svgH} viewBox={`0 0 ${svgW} ${svgH}`}
        style={{ display: "block", minWidth: svgW }}>
        <defs>
          <filter id="topo-shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="1" stdDeviation="2" floodColor="rgba(0,0,0,0.07)" />
          </filter>
          <marker id="topo-arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
            <polygon points="0 0, 8 3, 0 6" fill="#94a3b8" />
          </marker>
        </defs>

        <g transform={`translate(${PAD},${PAD})`}>
          {/* Edges */}
          {(edges || []).map((edge, idx) => {
            const src = positions[String(edge.sourceNodeId)];
            const tgt = positions[String(edge.targetNodeId)];
            if (!src || !tgt) return null;

            const x1 = src.x + NODE_W / 2;
            const y1 = src.y + NODE_H;
            const x2 = tgt.x + NODE_W / 2;
            const y2 = tgt.y;
            const dy = Math.abs(y2 - y1);
            const ctrl = Math.max(dy * 0.45, 20);
            const d = `M ${x1},${y1} C ${x1},${y1 + ctrl} ${x2},${y2 - ctrl} ${x2},${y2}`;

            return (
              <path key={edge.id ?? `e${idx}`} d={d}
                fill="none" stroke="#cbd5e1" strokeWidth={1.5}
                markerEnd="url(#topo-arrow)" />
            );
          })}

          {/* Nodes */}
          {(nodes || []).map((node) => {
            const pos = positions[String(node.id)];
            if (!pos) return null;
            const hasType = Boolean(node.type);
            const nameY = hasType ? pos.y + 32 : pos.y + NODE_H / 2 + 5;
            const typeY = pos.y + 16;
            const maxNameChars = 17;
            const label = node.name.length > maxNameChars
              ? node.name.slice(0, maxNameChars - 1) + "…"
              : node.name;
            const typeLabel = node.type && node.type.length > 16
              ? node.type.slice(0, 15) + "…"
              : node.type;

            return (
              <g key={node.id} title={node.description || node.name}>
                <rect x={pos.x} y={pos.y} width={NODE_W} height={NODE_H}
                  rx={6} ry={6}
                  fill="white" stroke="#e2e8f0" strokeWidth={1.5}
                  filter="url(#topo-shadow)" />
                {hasType && (
                  <text x={pos.x + NODE_W / 2} y={typeY}
                    textAnchor="middle" dominantBaseline="middle"
                    fill="#94a3b8" fontSize={10} fontWeight={500}>
                    {typeLabel}
                  </text>
                )}
                <text x={pos.x + NODE_W / 2} y={nameY}
                  textAnchor="middle" dominantBaseline="middle"
                  fill="#0f172a" fontSize={13} fontWeight={600}>
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

// ─── Topology Editor ─────────────────────────────────────────────────────────

let _topoIdCounter = 1;
function newTempId() { return `node-new-${_topoIdCounter++}`; }

function TopologyEditor({ topology, onChange }) {
  const { nodes, edges } = topology;

  const namedNodes = nodes.filter((n) => n.name.trim());
  const nodeOptions = namedNodes.map((n) => ({ value: n.tempId, label: n.name }));

  const addNode = () => {
    onChange({ nodes: [...nodes, { tempId: newTempId(), name: "", type: "", description: "" }], edges });
  };

  const updateNode = (tempId, field, value) => {
    const updatedNodes = nodes.map((n) => n.tempId === tempId ? { ...n, [field]: value } : n);
    // If node name changed, edges remain valid (they use tempId, not name)
    onChange({ nodes: updatedNodes, edges });
  };

  const removeNode = (tempId) => {
    onChange({
      nodes: nodes.filter((n) => n.tempId !== tempId),
      edges: edges.filter((e) => e.sourceTempId !== tempId && e.targetTempId !== tempId),
    });
  };

  const addEdge = () => {
    if (namedNodes.length < 2) return;
    const src = namedNodes[0].tempId;
    const tgt = namedNodes[1].tempId;
    const isDup = edges.some((e) => e.sourceTempId === src && e.targetTempId === tgt);
    if (!isDup) onChange({ nodes, edges: [...edges, { sourceTempId: src, targetTempId: tgt }] });
    else onChange({ nodes, edges: [...edges, { sourceTempId: src, targetTempId: tgt }] });
  };

  const updateEdge = (idx, field, value) => {
    onChange({ nodes, edges: edges.map((e, i) => i === idx ? { ...e, [field]: value } : e) });
  };

  const removeEdge = (idx) => {
    onChange({ nodes, edges: edges.filter((_, i) => i !== idx) });
  };

  // Live preview — map editor nodes/edges to viewer format
  const previewNodes = namedNodes.map((n) => ({ id: n.tempId, name: n.name, type: n.type }));
  const previewEdges = edges
    .filter((e) => e.sourceTempId && e.targetTempId && e.sourceTempId !== e.targetTempId)
    .map((e, i) => ({ id: `pe${i}`, sourceNodeId: e.sourceTempId, targetNodeId: e.targetTempId }));

  return (
    <div className="topo-editor">
      {/* ── Components ── */}
      <div className="topo-editor-block">
        <div className="topo-editor-block-header">
          <span className="topo-editor-block-title">Components</span>
          <button type="button" className="btn-outline btn-compact" onClick={addNode}>+ Add component</button>
        </div>

        {nodes.length === 0 ? (
          <p className="muted" style={{ fontSize: "0.875rem" }}>No components yet. Add the first one to start building the topology.</p>
        ) : (
          <div className="topo-node-list">
            <div className="topo-node-header">
              <span>Name *</span>
              <span>Type</span>
              <span />
            </div>
            {nodes.map((node) => (
              <div key={node.tempId} className="topo-node-row">
                <input
                  className="topo-input"
                  value={node.name}
                  onChange={(e) => updateNode(node.tempId, "name", e.target.value)}
                  placeholder="e.g. WAF"
                  maxLength={120}
                />
                <input
                  className="topo-input"
                  value={node.type || ""}
                  onChange={(e) => updateNode(node.tempId, "type", e.target.value)}
                  placeholder="e.g. API Gateway"
                  maxLength={80}
                />
                <button type="button" className="topo-remove-btn" onClick={() => removeNode(node.tempId)} title="Remove component">✕</button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Connections ── */}
      <div className="topo-editor-block">
        <div className="topo-editor-block-header">
          <span className="topo-editor-block-title">Connections</span>
          <button
            type="button"
            className="btn-outline btn-compact"
            onClick={addEdge}
            disabled={namedNodes.length < 2}
            title={namedNodes.length < 2 ? "Add at least 2 named components first" : undefined}
          >
            + Add connection
          </button>
        </div>

        {edges.length === 0 ? (
          <p className="muted" style={{ fontSize: "0.875rem" }}>
            {namedNodes.length < 2 ? "Add at least 2 components first." : "No connections yet."}
          </p>
        ) : (
          <div className="topo-edge-list">
            {edges.map((edge, idx) => (
              <div key={idx} className="topo-edge-row">
                <div className="topo-edge-select-wrap">
                  <select
                    className="topo-select"
                    value={edge.sourceTempId}
                    onChange={(e) => updateEdge(idx, "sourceTempId", e.target.value)}
                  >
                    {nodeOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </div>
                <span className="topo-edge-arrow">→</span>
                <div className="topo-edge-select-wrap">
                  <select
                    className="topo-select"
                    value={edge.targetTempId}
                    onChange={(e) => updateEdge(idx, "targetTempId", e.target.value)}
                  >
                    {nodeOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </div>
                <button type="button" className="topo-remove-btn" onClick={() => removeEdge(idx)} title="Remove connection">✕</button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Live preview ── */}
      {previewNodes.length > 0 && (
        <div className="topo-preview-block">
          <span className="topo-editor-block-title">Preview</span>
          <div style={{ marginTop: "0.5rem" }}>
            <TopologyViewer nodes={previewNodes} edges={previewEdges} compact />
          </div>
        </div>
      )}
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
          <h4>Linked deployments</h4>
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
          <p className="form-label" style={{ marginBottom: "0.5rem" }}>Linked deployments</p>
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
