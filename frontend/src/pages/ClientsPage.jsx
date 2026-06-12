import { useEffect, useState } from "react";
import {
  createClient,
  deleteClient,
  listApplicationServices,
  listClients,
  updateClient,
} from "../api";
import { useAuth } from "../context/AuthContext";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import PageTitle from "../components/common/PageTitle.jsx";

const STATUS_BADGE = {
  healthy: "pass",
  warning: "warning",
  critical: "fail",
  unknown: "pending",
};

function StatusBadge({ status }) {
  const variant = STATUS_BADGE[status] || "pending";
  return (
    <span className={`status-badge status-badge--${variant}`}>
      {status || "unknown"}
    </span>
  );
}

function ClientModal({ client, allServices, onClose, onSave, saving, error }) {
  const isEdit = Boolean(client?.id);
  const [name, setName] = useState(client?.name || "");
  const [contactPerson, setContactPerson] = useState(client?.contactPerson || "");
  const [email, setEmail] = useState(client?.email || "");
  const [phone, setPhone] = useState(client?.phone || "");
  const [notes, setNotes] = useState(client?.notes || "");
  const [selectedServiceIds, setSelectedServiceIds] = useState(
    (client?.services || []).map((s) => s.id)
  );

  const toggleService = (id) => {
    setSelectedServiceIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  };

  const handleSubmit = () => {
    if (!name.trim()) return;
    onSave({
      name: name.trim(),
      contactPerson: contactPerson.trim() || undefined,
      email: email.trim() || undefined,
      phone: phone.trim() || undefined,
      notes: notes.trim() || undefined,
      serviceIds: selectedServiceIds,
    });
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>{isEdit ? "Edit Client" : "New Client"}</h3>
          <p className="muted">
            {isEdit ? "Update client details and assigned services." : "Add a business client and assign application services."}
          </p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <section className="form-section">
          <h4>Client details</h4>
          <div className="form-grid">
            <label className="form-grid__full">
              Name *
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={120}
                placeholder="e.g. Acme Corp"
              />
            </label>
            <label className="form-grid__full">
              Contact person
              <input
                value={contactPerson}
                onChange={(e) => setContactPerson(e.target.value)}
                maxLength={255}
                placeholder="Full name"
              />
            </label>
            <label>
              Email
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="contact@example.com"
              />
            </label>
            <label>
              Phone
              <input
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                maxLength={64}
                placeholder="+1 555 0100"
              />
            </label>
            <label className="form-grid__full">
              Notes
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                rows={3}
                style={{ resize: "vertical" }}
                placeholder="Optional notes"
              />
            </label>
          </div>
        </section>

        <section className="form-section">
          <h4>Assigned services</h4>
          {allServices.length === 0 ? (
            <p className="muted">No application services available. Create one first.</p>
          ) : (
            <div className="form-grid">
              {allServices.map((svc) => (
                <label key={svc.id} className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={selectedServiceIds.includes(svc.id)}
                    onChange={() => toggleService(svc.id)}
                  />
                  {svc.name}
                  <span className={`status-badge status-badge--${STATUS_BADGE[svc.health] || "pending"}`} style={{ fontSize: "0.75rem", marginLeft: "0.4rem" }}>
                    {svc.health || "unknown"}
                  </span>
                </label>
              ))}
            </div>
          )}
        </section>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="button" className="primary" onClick={handleSubmit} disabled={saving || !name.trim()}>
            {saving ? "Saving…" : isEdit ? "Save changes" : "Create client"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ClientDetailPanel({ client, onEdit, onDelete, canEdit, canDelete }) {
  return (
    <div className="card" style={{ padding: "1.25rem" }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "1rem", marginBottom: "1rem" }}>
        <div>
          <h3 style={{ margin: 0 }}>{client.name}</h3>
          {client.contactPerson && (
            <p className="muted" style={{ marginTop: "0.25rem", fontSize: "0.875rem" }}>
              {client.contactPerson}
            </p>
          )}
        </div>
        <StatusBadge status={client.status} />
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", fontSize: "0.875rem", marginBottom: "1.25rem" }}>
        {client.email && (
          <div>
            <span className="muted">Email: </span>
            <a href={`mailto:${client.email}`}>{client.email}</a>
          </div>
        )}
        {client.phone && (
          <div>
            <span className="muted">Phone: </span>
            <span>{client.phone}</span>
          </div>
        )}
        {client.notes && (
          <div>
            <span className="muted">Notes: </span>
            <span>{client.notes}</span>
          </div>
        )}
        <div className="muted">
          {client.serviceCount ?? 0} service{client.serviceCount !== 1 ? "s" : ""}
          {client.createdAt && ` · Created ${new Date(client.createdAt).toLocaleDateString()}`}
        </div>
      </div>

      {(client.services || []).length > 0 && (
        <div style={{ marginBottom: "1.25rem" }}>
          <p className="form-label" style={{ marginBottom: "0.5rem" }}>Assigned services</p>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
            {client.services.map((svc) => (
              <div key={svc.id} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.875rem" }}>
                <span>{svc.name}</span>
                <StatusBadge status={svc.health} />
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: "0.75rem" }}>
        {canEdit && (
          <button className="btn-outline btn-compact" onClick={onEdit}>Edit</button>
        )}
        {canDelete && (
          <button className="btn-outline btn-compact danger" onClick={onDelete}>Delete</button>
        )}
      </div>
    </div>
  );
}

export default function ClientsPage() {
  const { hasPermission } = useAuth();
  const canView = hasPermission("clients:view");
  const canCreate = hasPermission("clients:create");
  const canUpdate = hasPermission("clients:update");
  const canDelete = hasPermission("clients:delete");

  const [clients, setClients] = useState([]);
  const [allServices, setAllServices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [selectedId, setSelectedId] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingClient, setEditingClient] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [deleting, setDeleting] = useState(false);

  const selectedClient = clients.find((c) => c.id === selectedId) || null;

  const loadData = async () => {
    setLoading(true);
    setError("");
    try {
      const [clientsRes, servicesRes] = await Promise.all([
        listClients(),
        listApplicationServices(),
      ]);
      setClients(clientsRes.items || []);
      setAllServices(servicesRes.items || []);
    } catch (err) {
      setError(err.message || "Failed to load clients.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadData(); }, []);

  const openCreate = () => {
    setEditingClient(null);
    setSaveError("");
    setModalOpen(true);
  };

  const openEdit = (client) => {
    setEditingClient(client);
    setSaveError("");
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setEditingClient(null);
    setSaveError("");
  };

  const handleSave = async (payload) => {
    setSaving(true);
    setSaveError("");
    try {
      if (editingClient?.id) {
        const updated = await updateClient(editingClient.id, payload);
        setClients((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
        setSelectedId(updated.id);
      } else {
        const created = await createClient(payload);
        setClients((prev) => [...prev, created]);
        setSelectedId(created.id);
      }
      closeModal();
    } catch (err) {
      setSaveError(err.message || "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (client) => {
    if (!window.confirm(`Delete "${client.name}"? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      await deleteClient(client.id);
      setClients((prev) => prev.filter((c) => c.id !== client.id));
      if (selectedId === client.id) setSelectedId(null);
    } catch (err) {
      setError(err.message || "Delete failed.");
    } finally {
      setDeleting(false);
    }
  };

  if (!canView) return <AccessDeniedPage />;

  const filtered = clients.filter((c) => {
    if (statusFilter !== "all" && c.status !== statusFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (
        c.name.toLowerCase().includes(q) ||
        (c.contactPerson || "").toLowerCase().includes(q) ||
        (c.email || "").toLowerCase().includes(q)
      );
    }
    return true;
  });

  return (
    <div className="ops-page">
      <PageTitle
        title="Clients"
        subtitle="Business clients with assigned application services."
        actionLabel={canCreate ? "New client" : undefined}
        onAction={canCreate ? openCreate : undefined}
      />

      {error && <ErrorBanner message={error} />}

      <div className="user-filters" style={{ marginBottom: "1rem" }}>
        <input
          type="search"
          className="form-input"
          placeholder="Search by name, contact, or email…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ maxWidth: 280 }}
        />
        <select
          className="form-select"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="all">All statuses</option>
          <option value="healthy">Healthy</option>
          <option value="warning">Warning</option>
          <option value="critical">Critical</option>
          <option value="unknown">Unknown</option>
        </select>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: selectedClient ? "1fr 1fr" : "1fr", gap: "1.25rem", alignItems: "start" }}>
        <div>
          {loading ? (
            <LoadingState label="Loading clients…" />
          ) : filtered.length === 0 ? (
            <EmptyState
              message="No clients found."
              hint={clients.length > 0 ? "Try adjusting the search or filter." : "Create your first client to get started."}
            />
          ) : (
            <div className="table-shell">
              <div className="table-scroll-region">
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Status</th>
                      <th>Services</th>
                      <th>Contact</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((c) => (
                      <tr
                        key={c.id}
                        className={selectedId === c.id ? "table-row--selected" : ""}
                        style={{ cursor: "pointer" }}
                        onClick={() => setSelectedId(c.id === selectedId ? null : c.id)}
                      >
                        <td>
                          <strong>{c.name}</strong>
                          {c.email && (
                            <div className="muted" style={{ fontSize: "0.8rem" }}>{c.email}</div>
                          )}
                        </td>
                        <td><StatusBadge status={c.status} /></td>
                        <td>{c.serviceCount ?? 0}</td>
                        <td className="muted" style={{ fontSize: "0.8rem" }}>
                          {c.contactPerson || "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        {selectedClient && (
          <ClientDetailPanel
            client={selectedClient}
            onEdit={() => openEdit(selectedClient)}
            onDelete={() => handleDelete(selectedClient)}
            canEdit={canUpdate}
            canDelete={canDelete && !deleting}
          />
        )}
      </div>

      {modalOpen && (
        <ClientModal
          client={editingClient}
          allServices={allServices}
          onClose={closeModal}
          onSave={handleSave}
          saving={saving}
          error={saveError}
        />
      )}
    </div>
  );
}
