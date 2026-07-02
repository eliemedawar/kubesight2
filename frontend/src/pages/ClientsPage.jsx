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
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import SearchableSelect from "../components/common/SearchableSelect.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import ClientDetailModal from "../components/clients/ClientDetailModal.jsx";

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

export default function ClientsPage({ clusters = [] }) {
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
        <SearchableSelect
          className="form-select"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="all">All statuses</option>
          <option value="healthy">Healthy</option>
          <option value="warning">Warning</option>
          <option value="critical">Critical</option>
          <option value="unknown">Unknown</option>
        </SearchableSelect>
      </div>

      <div>
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

      </div>

      {selectedClient && (
        <ClientDetailModal
          client={selectedClient}
          clusters={clusters}
          canUpdate={canUpdate}
          canDelete={canDelete && !deleting}
          onEditClient={() => { const c = selectedClient; setSelectedId(null); openEdit(c); }}
          onDeleteClient={() => { const c = selectedClient; setSelectedId(null); handleDelete(c); }}
          onClose={() => setSelectedId(null)}
        />
      )}

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
