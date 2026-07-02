import { useEffect, useState, useCallback } from "react";
import {
  listClientServices,
  saveClientServiceConnection,
  getClientServiceTopology,
  deleteClientServiceConnection,
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

function TopologyTab({ clientId, services, selectedServiceId, onSelectService }) {
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

  if (services.length === 0) {
    return <EmptyState message="No services linked to this client." hint="Assign a service to configure connectivity." />;
  }

  const conn = data?.connection;

  return (
    <div>
      <div style={{ maxWidth: 340, marginBottom: "1rem" }}>
        <p className="form-label" style={{ marginBottom: "0.35rem" }}>Service</p>
        <SearchableSelect
          options={services.map((s) => ({ value: String(s.serviceId), label: s.serviceName }))}
          value={selectedServiceId ? String(selectedServiceId) : ""}
          onChange={(e) => onSelectService(e.target.value ? Number(e.target.value) : null)}
          placeholder="Select a service…"
        />
      </div>

      {!selectedServiceId ? (
        <p className="muted">Select a service to view its client access topology.</p>
      ) : loading ? (
        <LoadingState label="Composing topology…" />
      ) : error ? (
        <p className="banner-message error">{error}</p>
      ) : data ? (
        <>
          <div className="access-summary" style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem 1.5rem", marginBottom: "1rem", fontSize: "0.85rem" }}>
            <div><span className="muted">Client: </span><strong>{data.client?.name}</strong></div>
            <div><span className="muted">Source IP: </span>{orDash(conn?.sourceIp)}</div>
            <div><span className="muted">Transport: </span>{orDash(conn?.transportType)}</div>
            <div><span className="muted">Destination IP: </span>{orDash(conn?.destinationIp)}</div>
          </div>
          <TopologyViewer nodes={data.topology?.nodes} edges={data.topology?.edges} fillWidth />
        </>
      ) : null}
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
    const onKey = (e) => { if (e.key === "Escape" && !editingConn) onClose?.(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, editingConn]);

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
    </div>
  );
}
