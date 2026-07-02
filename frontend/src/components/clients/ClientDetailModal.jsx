import { useEffect, useState, useCallback } from "react";
import {
  listClientServices,
  saveClientServiceConnection,
  getClientServiceTopology,
  deleteClientServiceConnection,
  listServiceEgressNodes,
  saveClientServiceEgressConnection,
  getClientServiceEgressTopology,
  deleteClientServiceEgressConnection,
} from "../../api";
import TopologyViewer from "../common/TopologyViewer.jsx";
import SearchableSelect from "../common/SearchableSelect.jsx";
import LoadingState from "../common/LoadingState.jsx";
import EmptyState from "../common/EmptyState.jsx";
import EditConnectionModal from "./EditConnectionModal.jsx";

const HEALTH_BADGE = { healthy: "pass", warning: "warning", critical: "fail", unknown: "pending" };
const CONN_STATUS_BADGE = { active: "pass", degraded: "warning", inactive: "fail", planned: "pending" };

function HealthBadge({ health }) {
  return <span className={`status-badge status-badge--${HEALTH_BADGE[health] || "pending"}`}>{health || "unknown"}</span>;
}

function ConnStatusBadge({ status }) {
  if (!status) return <span className="muted">—</span>;
  return <span className={`status-badge status-badge--${CONN_STATUS_BADGE[status] || "pending"}`}>{status}</span>;
}

function orDash(value) {
  return value ? value : <span className="muted">Not configured</span>;
}

function tabStyle(active) {
  return {
    background: "none",
    border: "none",
    padding: "0.5rem 0.85rem",
    cursor: "pointer",
    fontWeight: 600,
    fontSize: "0.875rem",
    color: active ? "var(--accent-strong)" : "var(--text-muted)",
    borderBottom: active ? "2px solid var(--accent)" : "2px solid transparent",
    marginBottom: "-1px",
  };
}

// ─── Connectivity Topology tab ────────────────────────────────────────────────

function DirectionToggle({ direction, onChange }) {
  const opts = [
    { id: "inbound", label: "Inbound (Client → Service)" },
    { id: "outbound", label: "Outbound (Service → Client)" },
  ];
  return (
    <div role="tablist" aria-label="Topology direction"
      style={{ display: "inline-flex", border: "1px solid var(--border)", borderRadius: "0.5rem", overflow: "hidden", marginBottom: "1rem" }}>
      {opts.map((o) => {
        const active = direction === o.id;
        return (
          <button key={o.id} type="button" role="tab" aria-selected={active}
            onClick={() => onChange(o.id)}
            style={{
              background: active ? "var(--accent)" : "transparent",
              color: active ? "var(--accent-contrast, #fff)" : "var(--text-muted)",
              border: "none", padding: "0.4rem 0.9rem", cursor: "pointer",
              fontWeight: 600, fontSize: "0.8125rem",
            }}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

// Inbound: Client → Transport → Service → … → Deployments.
function InboundTopology({ clientId, selectedServiceId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!selectedServiceId) { setData(null); return; }
    let cancelled = false;
    setLoading(true);
    setError("");
    getClientServiceTopology(clientId, selectedServiceId)
      .then((res) => { if (!cancelled) setData(res); })
      .catch((err) => { if (!cancelled) setError(err.message || "Failed to load topology."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [clientId, selectedServiceId]);

  if (!selectedServiceId) return <p className="muted">Select a service to view its client access topology.</p>;
  if (loading) return <LoadingState label="Composing topology…" />;
  if (error) return <p className="banner-message error">{error}</p>;
  if (!data) return null;

  const conn = data.connection;
  return (
    <>
      <div className="access-summary" style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem 1.5rem", marginBottom: "1rem", fontSize: "0.85rem" }}>
        <div><span className="muted">Client: </span><strong>{data.client?.name}</strong></div>
        <div><span className="muted">Source IP: </span>{orDash(conn?.sourceIp)}</div>
        <div><span className="muted">Transport: </span>{orDash(conn?.transportType)}</div>
        <div><span className="muted">Destination IP: </span>{orDash(conn?.destinationIp)}</div>
      </div>
      <TopologyViewer nodes={data.topology?.nodes} edges={data.topology?.edges} fillWidth />
    </>
  );
}

// Outbound: selected Deployment → … → Transport → Client (reverse chain).
function OutboundTopology({ clientId, selectedServiceId, canUpdate, reloadKey, onEditEgress, onRemoveEgress }) {
  const [nodes, setNodes] = useState([]);
  const [hasTopology, setHasTopology] = useState(true);
  const [selectedNodeRef, setSelectedNodeRef] = useState(null);
  const [nodesLoading, setNodesLoading] = useState(false);
  const [nodesError, setNodesError] = useState("");

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // Load the service's egress-eligible deployment nodes.
  useEffect(() => {
    if (!selectedServiceId) { setNodes([]); setSelectedNodeRef(null); return; }
    let cancelled = false;
    setNodesLoading(true);
    setNodesError("");
    listServiceEgressNodes(clientId, selectedServiceId)
      .then((res) => {
        if (cancelled) return;
        const items = res.items || [];
        setNodes(items);
        setHasTopology(!!res.hasTopology);
        setSelectedNodeRef((prev) => {
          if (prev && items.some((n) => n.nodeRef === prev)) return prev;
          return items.length ? items[0].nodeRef : null;
        });
      })
      .catch((err) => { if (!cancelled) setNodesError(err.message || "Failed to load deployments."); })
      .finally(() => { if (!cancelled) setNodesLoading(false); });
    return () => { cancelled = true; };
  }, [clientId, selectedServiceId, reloadKey]);

  // Compose the reverse topology for the selected deployment.
  useEffect(() => {
    if (!selectedServiceId || !selectedNodeRef) { setData(null); return; }
    let cancelled = false;
    setLoading(true);
    setError("");
    getClientServiceEgressTopology(clientId, selectedServiceId, selectedNodeRef)
      .then((res) => { if (!cancelled) setData(res); })
      .catch((err) => { if (!cancelled) setError(err.message || "Failed to load topology."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [clientId, selectedServiceId, selectedNodeRef, reloadKey]);

  if (!selectedServiceId) return <p className="muted">Select a service to view its service-to-client topology.</p>;
  if (nodesLoading) return <LoadingState label="Loading deployments…" />;
  if (nodesError) return <p className="banner-message error">{nodesError}</p>;
  if (!hasTopology || nodes.length === 0) {
    return <EmptyState message="This service has no deployments to configure egress from." hint="Define the service topology first." />;
  }

  const selectedNode = nodes.find((n) => n.nodeRef === selectedNodeRef);
  const conn = data?.connection;

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: "1rem", marginBottom: "1rem" }}>
        <div style={{ minWidth: 240 }}>
          <p className="form-label" style={{ marginBottom: "0.35rem" }}>Deployment (source)</p>
          <SearchableSelect
            options={nodes.map((n) => ({ value: n.nodeRef, label: n.nodeName }))}
            value={selectedNodeRef || ""}
            onChange={(e) => setSelectedNodeRef(e.target.value || null)}
            placeholder="Select a deployment…"
          />
        </div>
        {canUpdate && selectedNode && (
          <div style={{ display: "flex", gap: "0.35rem" }}>
            <button type="button" className="primary btn-compact"
              onClick={() => onEditEgress(selectedNode)}>
              {selectedNode.connection ? "Edit Connection" : "Configure"}
            </button>
            {selectedNode.connection && (
              <button type="button" className="btn-ghost btn-compact danger"
                onClick={() => onRemoveEgress(selectedNode)}>
                Remove
              </button>
            )}
          </div>
        )}
      </div>

      {loading ? (
        <LoadingState label="Composing topology…" />
      ) : error ? (
        <p className="banner-message error">{error}</p>
      ) : data ? (
        <>
          <div className="access-summary" style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem 1.5rem", marginBottom: "1rem", fontSize: "0.85rem" }}>
            <div><span className="muted">Deployment: </span><strong>{data.node?.name}</strong></div>
            <div><span className="muted">Source IP: </span>{orDash(conn?.sourceIp)}</div>
            <div><span className="muted">Transport: </span>{orDash(conn?.transportType)}</div>
            <div><span className="muted">Destination IP: </span>{orDash(conn?.destinationIp)}</div>
            <div><span className="muted">Client: </span><strong>{data.client?.name}</strong></div>
          </div>
          <TopologyViewer nodes={data.topology?.nodes} edges={data.topology?.edges} fillWidth />
        </>
      ) : null}
    </div>
  );
}

function TopologyTab({ clientId, services, selectedServiceId, onSelectService, canUpdate, egressReloadKey, onEditEgress, onRemoveEgress }) {
  const [direction, setDirection] = useState("inbound");

  if (services.length === 0) {
    return <EmptyState message="No services linked to this client." hint="Assign a service to configure connectivity." />;
  }

  return (
    <div>
      <DirectionToggle direction={direction} onChange={setDirection} />

      <div style={{ maxWidth: 340, marginBottom: "1rem" }}>
        <p className="form-label" style={{ marginBottom: "0.35rem" }}>Service</p>
        <SearchableSelect
          options={services.map((s) => ({ value: String(s.serviceId), label: s.serviceName }))}
          value={selectedServiceId ? String(selectedServiceId) : ""}
          onChange={(e) => onSelectService(e.target.value ? Number(e.target.value) : null)}
          placeholder="Select a service…"
        />
      </div>

      {direction === "inbound" ? (
        <InboundTopology clientId={clientId} selectedServiceId={selectedServiceId} />
      ) : (
        <OutboundTopology
          clientId={clientId}
          selectedServiceId={selectedServiceId}
          canUpdate={canUpdate}
          reloadKey={egressReloadKey}
          onEditEgress={(node) => onEditEgress(selectedServiceId, node)}
          onRemoveEgress={(node) => onRemoveEgress(selectedServiceId, node)}
        />
      )}
    </div>
  );
}

// ─── Access Details tab ───────────────────────────────────────────────────────

function AccessDetailsTab({ services }) {
  if (services.length === 0) {
    return <EmptyState message="No services linked to this client." />;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {services.map((svc) => {
        const c = svc.connection || {};
        return (
          <div key={svc.serviceId} className="card" style={{ padding: "1rem" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem", marginBottom: "0.75rem" }}>
              <strong>{svc.serviceName}</strong>
              <ConnStatusBadge status={c.status} />
            </div>
            <div className="form-grid" style={{ fontSize: "0.85rem" }}>
              <div><span className="muted">Source IP: </span>{orDash(c.sourceIp)}</div>
              <div><span className="muted">Destination IP: </span>{orDash(c.destinationIp)}</div>
              <div><span className="muted">Transport: </span>{orDash(c.transportType)}</div>
              <div><span className="muted">Transport details: </span>{orDash(c.transportName)}</div>
              <div><span className="muted">Active: </span>{svc.connection ? (c.isActive ? "Yes" : "No") : <span className="muted">Not configured</span>}</div>
              {c.transportNotes && (
                <div className="form-grid__full"><span className="muted">Notes: </span>{c.transportNotes}</div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Services tab ─────────────────────────────────────────────────────────────

function Field({ label, children }) {
  return (
    <div>
      <div className="muted" style={{ fontSize: "0.7rem", textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
      <div style={{ fontSize: "0.85rem" }}>{children}</div>
    </div>
  );
}

function ServicesTab({ services, canUpdate, onViewTopology, onEditConnection, onRemove }) {
  if (services.length === 0) {
    return <EmptyState message="No services linked to this client." hint="Edit the client to assign application services." />;
  }
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.85rem" }}>
      {canUpdate && (
        <p className="muted" style={{ fontSize: "0.8125rem", margin: 0 }}>
          Click a service card (or “Configure”) to set its client-specific connectivity.
        </p>
      )}
      {services.map((svc) => {
        const c = svc.connection || {};
        return (
          <div
            key={svc.serviceId}
            className="card"
            style={{ padding: "0.9rem 1rem", cursor: canUpdate ? "pointer" : undefined }}
            onClick={canUpdate ? () => onEditConnection(svc) : undefined}
            title={canUpdate ? "Configure connectivity" : undefined}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.75rem", flexWrap: "wrap", marginBottom: "0.75rem" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap" }}>
                <strong>{svc.serviceName}</strong>
                <HealthBadge health={svc.health} />
                <ConnStatusBadge status={svc.connection ? c.status : null} />
              </div>
              <div style={{ display: "flex", gap: "0.35rem", flexWrap: "wrap" }}>
                <button type="button" className="btn-outline btn-compact" onClick={stop(() => onViewTopology(svc.serviceId))}>
                  View Topology
                </button>
                {canUpdate && (
                  <button type="button" className="primary btn-compact" onClick={stop(() => onEditConnection(svc))}>
                    {svc.connection ? "Edit Connection" : "Configure"}
                  </button>
                )}
                {canUpdate && svc.connection && (
                  <button type="button" className="btn-ghost btn-compact danger" onClick={stop(() => onRemove(svc))}>
                    Remove
                  </button>
                )}
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: "0.6rem 1rem" }}>
              <Field label="Source IP">{orDash(c.sourceIp)}</Field>
              <Field label="Destination IP">{orDash(c.destinationIp)}</Field>
              <Field label="Transport">{orDash(c.transportType)}</Field>
              <Field label="Transport details">{orDash(c.transportName)}</Field>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Client detail modal ──────────────────────────────────────────────────────

export default function ClientDetailModal({
  client,
  canUpdate,
  canDelete,
  onEditClient,
  onDeleteClient,
  onClose,
}) {
  const [tab, setTab] = useState("overview");
  const [services, setServices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedServiceId, setSelectedServiceId] = useState(null);

  const [editingConn, setEditingConn] = useState(null); // { serviceId, serviceName, connection }
  const [savingConn, setSavingConn] = useState(false);
  const [connError, setConnError] = useState("");

  // Egress (Service → Client) editing state.
  const [editingEgress, setEditingEgress] = useState(null); // { serviceId, serviceName, nodeRef, nodeName, connection }
  const [savingEgress, setSavingEgress] = useState(false);
  const [egressError, setEgressError] = useState("");
  const [egressReloadKey, setEgressReloadKey] = useState(0);

  const loadServices = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await listClientServices(client.id);
      setServices(res.items || []);
    } catch (err) {
      setError(err.message || "Failed to load services.");
    } finally {
      setLoading(false);
    }
  }, [client.id]);

  useEffect(() => { loadServices(); }, [loadServices]);

  // Close on Escape (only when no nested modal is open).
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && !editingConn && !editingEgress) onClose?.(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, editingConn, editingEgress]);

  const handleViewTopology = (serviceId) => {
    setSelectedServiceId(serviceId);
    setTab("topology");
  };

  const handleSaveConnection = async (payload) => {
    setSavingConn(true);
    setConnError("");
    try {
      await saveClientServiceConnection(client.id, editingConn.serviceId, payload);
      setEditingConn(null);
      await loadServices();
    } catch (err) {
      setConnError(err.message || "Save failed.");
    } finally {
      setSavingConn(false);
    }
  };

  const handleRemove = async (svc) => {
    if (!window.confirm(`Remove connectivity for "${svc.serviceName}"? The service stays linked to the client.`)) return;
    try {
      await deleteClientServiceConnection(client.id, svc.serviceId);
      await loadServices();
    } catch (err) {
      setError(err.message || "Remove failed.");
    }
  };

  const serviceNameOf = (serviceId) =>
    services.find((s) => s.serviceId === serviceId)?.serviceName || "";

  const handleEditEgress = (serviceId, node) => {
    setEgressError("");
    setEditingEgress({
      serviceId,
      serviceName: serviceNameOf(serviceId),
      nodeRef: node.nodeRef,
      nodeName: node.nodeName,
      connection: node.connection,
    });
  };

  const handleSaveEgress = async (payload) => {
    setSavingEgress(true);
    setEgressError("");
    try {
      await saveClientServiceEgressConnection(
        client.id, editingEgress.serviceId, editingEgress.nodeRef, payload
      );
      setEditingEgress(null);
      setEgressReloadKey((k) => k + 1);
    } catch (err) {
      setEgressError(err.message || "Save failed.");
    } finally {
      setSavingEgress(false);
    }
  };

  const handleRemoveEgress = async (serviceId, node) => {
    if (!window.confirm(`Remove egress connectivity for "${node.nodeName}"?`)) return;
    try {
      await deleteClientServiceEgressConnection(client.id, serviceId, node.nodeRef);
      setEgressReloadKey((k) => k + 1);
    } catch (err) {
      setError(err.message || "Remove failed.");
    }
  };

  const TABS = [
    { id: "overview", label: "Overview" },
    { id: "services", label: "Services" },
    { id: "topology", label: "Connectivity Topology" },
    { id: "access", label: "Access Details" },
  ];

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide service-detail-modal" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="service-detail-modal__head">
          <div>
            <h3 style={{ margin: 0 }}>{client.name}</h3>
            {client.contactPerson && (
              <p className="muted" style={{ marginTop: "0.25rem", fontSize: "0.875rem" }}>{client.contactPerson}</p>
            )}
          </div>
          <button type="button" className="btn-ghost modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="service-detail-tabs" role="tablist"
          style={{ display: "flex", gap: "0.25rem", borderBottom: "1px solid var(--border)", margin: "0.75rem 0 1rem", flexWrap: "wrap" }}>
          {TABS.map((t) => (
            <button key={t.id} type="button" role="tab" aria-selected={tab === t.id}
              onClick={() => setTab(t.id)} style={tabStyle(tab === t.id)}>
              {t.label}
            </button>
          ))}
        </div>

        {error && <p className="banner-message error">{error}</p>}

        {tab === "overview" && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", fontSize: "0.875rem" }}>
            {client.email && <div><span className="muted">Email: </span><a href={`mailto:${client.email}`}>{client.email}</a></div>}
            {client.phone && <div><span className="muted">Phone: </span>{client.phone}</div>}
            {client.notes && <div><span className="muted">Notes: </span>{client.notes}</div>}
            <div className="muted">
              {services.length} service{services.length !== 1 ? "s" : ""}
              {client.createdAt && ` · Created ${new Date(client.createdAt).toLocaleDateString()}`}
            </div>
            <div style={{ display: "flex", gap: "0.75rem", marginTop: "0.75rem" }}>
              {canUpdate && <button className="btn-outline btn-compact" onClick={onEditClient}>Edit client</button>}
              {canDelete && <button className="btn-outline btn-compact danger" onClick={onDeleteClient}>Delete client</button>}
            </div>
          </div>
        )}

        {loading && tab !== "overview" ? (
          <LoadingState label="Loading services…" />
        ) : (
          <>
            {tab === "services" && (
              <ServicesTab
                services={services}
                canUpdate={canUpdate}
                onViewTopology={handleViewTopology}
                onEditConnection={(svc) => { setConnError(""); setEditingConn({ serviceId: svc.serviceId, serviceName: svc.serviceName, connection: svc.connection }); }}
                onRemove={handleRemove}
              />
            )}
            {tab === "topology" && (
              <TopologyTab
                clientId={client.id}
                services={services}
                selectedServiceId={selectedServiceId}
                onSelectService={setSelectedServiceId}
                canUpdate={canUpdate}
                egressReloadKey={egressReloadKey}
                onEditEgress={handleEditEgress}
                onRemoveEgress={handleRemoveEgress}
              />
            )}
            {tab === "access" && (
              <AccessDetailsTab services={services} />
            )}
          </>
        )}

        <div className="modal-actions">
          <button className="btn-outline btn-compact" onClick={onClose}>Close</button>
        </div>
      </div>

      {editingConn && (
        <EditConnectionModal
          clientName={client.name}
          serviceName={editingConn.serviceName}
          connection={editingConn.connection}
          onClose={() => setEditingConn(null)}
          onSave={handleSaveConnection}
          saving={savingConn}
          error={connError}
        />
      )}

      {editingEgress && (
        <EditConnectionModal
          heading="Edit Egress Connection"
          subtitle={<>Service-to-client connectivity for <strong>{editingEgress.nodeName}</strong> → <strong>{client.name}</strong>.</>}
          connection={editingEgress.connection}
          onClose={() => setEditingEgress(null)}
          onSave={handleSaveEgress}
          saving={savingEgress}
          error={egressError}
        />
      )}
    </div>
  );
}
