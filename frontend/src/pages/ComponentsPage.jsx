import { useEffect, useMemo, useState } from "react";
import {
  checkComponentHealth,
  createComponent,
  deleteComponent,
  getComponent,
  getBaseUrl,
  listComponents,
  updateComponent,
} from "../api";
import { useAuth } from "../context/AuthContext";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import SearchableSelect from "../components/common/SearchableSelect.jsx";

const STATUS_BADGE = { healthy: "pass", degraded: "warning", unhealthy: "fail", unknown: "pending" };
const STATUS_LABEL = { healthy: "Healthy", degraded: "Degraded", unhealthy: "Unhealthy", unknown: "Unknown" };

const CHECK_TYPES = [
  { value: "none", label: "None (manual)" },
  { value: "http", label: "HTTP / API" },
  { value: "tcp", label: "TCP" },
  { value: "webhook", label: "Webhook (heartbeat)" },
];
const CHECK_TYPE_LABEL = {
  none: "None",
  http: "HTTP / API",
  tcp: "TCP",
  webhook: "Webhook",
};

function StatusBadge({ status }) {
  const key = STATUS_BADGE[status] ? status : "unknown";
  return <span className={`status-badge status-badge--${STATUS_BADGE[key]}`}>{STATUS_LABEL[key]}</span>;
}

function fmtDate(value) {
  if (!value) return "Never";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

// ─── Modal ────────────────────────────────────────────────────────────────────

function ComponentModal({ component, onClose, onSave, saving, error }) {
  const isEdit = Boolean(component?.id);
  const [name, setName] = useState(component?.name || "");
  const [category, setCategory] = useState(component?.category || "");
  const [description, setDescription] = useState(component?.description || "");
  const [checkType, setCheckType] = useState(component?.checkType || "none");
  const [healthCheckUrl, setHealthCheckUrl] = useState(component?.healthCheckUrl || "");
  const [tcpHost, setTcpHost] = useState(component?.tcpHost || "");
  const [tcpPort, setTcpPort] = useState(component?.tcpPort != null ? String(component.tcpPort) : "");
  const [interval, setInterval] = useState(
    component?.heartbeatIntervalSeconds != null ? String(component.heartbeatIntervalSeconds) : "300"
  );
  const [localError, setLocalError] = useState("");
  const [copied, setCopied] = useState(false);

  const webhookUrl =
    isEdit && component?.checkType === "webhook" && component?.webhookToken
      ? `${getBaseUrl()}/api/topology-components/${component.id}/heartbeat?token=${component.webhookToken}`
      : "";

  const validate = () => {
    if (!name.trim()) return "Component name is required.";
    if (checkType === "http") {
      if (!healthCheckUrl.trim()) return "A health check URL is required for an HTTP check.";
      if (!/^https?:\/\//i.test(healthCheckUrl.trim())) return "Health check URL must start with http:// or https://.";
    }
    if (checkType === "tcp") {
      if (!tcpHost.trim()) return "A host is required for a TCP check.";
      const p = Number(tcpPort);
      if (!p || p < 1 || p > 65535) return "A valid TCP port (1–65535) is required.";
    }
    if (checkType === "webhook") {
      const i = Number(interval);
      if (!i || i < 10) return "Heartbeat interval must be at least 10 seconds.";
    }
    return "";
  };

  const handleSubmit = () => {
    const v = validate();
    if (v) { setLocalError(v); return; }
    setLocalError("");
    onSave({
      name: name.trim(),
      category: category.trim(),
      description: description.trim(),
      checkType,
      healthCheckUrl: checkType === "http" ? healthCheckUrl.trim() : "",
      tcpHost: checkType === "tcp" ? tcpHost.trim() : "",
      tcpPort: checkType === "tcp" ? Number(tcpPort) : null,
      heartbeatIntervalSeconds: checkType === "webhook" ? Number(interval) : null,
    });
  };

  const copyWebhook = () => {
    navigator.clipboard?.writeText(webhookUrl).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>{isEdit ? "Edit Component" : "New Component"}</h3>
          <p className="muted">A reusable building block (e.g. WAF, API Gateway) you can drop into a service topology.</p>
        </div>

        {(localError || error) && <p className="banner-message error">{localError || error}</p>}

        <section className="form-section">
          <div className="form-grid">
            <label className="form-grid__full">
              Name *
              <input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} placeholder="e.g. WAF" />
            </label>
            <label>
              Category
              <input value={category} onChange={(e) => setCategory(e.target.value)} maxLength={80} placeholder="e.g. Security" />
            </label>
            <label>
              Health check
              <SearchableSelect
                options={CHECK_TYPES}
                value={checkType}
                onChange={(e) => setCheckType(e.target.value)}
              />
            </label>
            <label className="form-grid__full">
              Description
              <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2}
                style={{ resize: "vertical" }} placeholder="What this component does…" />
            </label>

            {checkType === "http" && (
              <label className="form-grid__full">
                Health check URL *
                <input value={healthCheckUrl} onChange={(e) => setHealthCheckUrl(e.target.value)}
                  maxLength={512} placeholder="https://waf.example.com/healthz" />
              </label>
            )}

            {checkType === "tcp" && (
              <>
                <label>
                  Host *
                  <input value={tcpHost} onChange={(e) => setTcpHost(e.target.value)} maxLength={253} placeholder="waf.internal" />
                </label>
                <label>
                  Port *
                  <input type="number" min="1" max="65535" value={tcpPort}
                    onChange={(e) => setTcpPort(e.target.value)} placeholder="443" />
                </label>
              </>
            )}

            {checkType === "webhook" && (
              <label>
                Heartbeat interval (seconds) *
                <input type="number" min="10" value={interval}
                  onChange={(e) => setInterval(e.target.value)} placeholder="300" />
              </label>
            )}
          </div>

          {checkType === "webhook" && (
            <div className="banner-message info" style={{ marginTop: "0.75rem" }}>
              {webhookUrl ? (
                <>
                  <p style={{ margin: "0 0 0.35rem", fontWeight: 600 }}>Heartbeat webhook</p>
                  <p className="muted" style={{ margin: "0 0 0.4rem", fontSize: "0.8rem" }}>
                    Have your monitor POST to this URL within the interval to keep the component healthy.
                  </p>
                  <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
                    <code style={{ flex: 1, wordBreak: "break-all", fontSize: "0.75rem" }}>{webhookUrl}</code>
                    <button type="button" className="btn-outline btn-compact" onClick={copyWebhook}>{copied ? "Copied" : "Copy"}</button>
                  </div>
                </>
              ) : (
                <p className="muted" style={{ margin: 0, fontSize: "0.8rem" }}>
                  The heartbeat URL and token are generated after you save this component.
                </p>
              )}
            </div>
          )}
        </section>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>Cancel</button>
          <button type="button" className="primary" onClick={handleSubmit} disabled={saving}>
            {saving ? "Saving…" : isEdit ? "Save changes" : "Create component"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ComponentsPage() {
  const { hasPermission } = useAuth();
  const canView = hasPermission("components:view");
  const canCreate = hasPermission("components:create");
  const canUpdate = hasPermission("components:update");
  const canDelete = hasPermission("components:delete");
  const canCheck = hasPermission("components:check");

  const [components, setComponents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [checkingId, setCheckingId] = useState(null);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await listComponents();
      setComponents(res.items || []);
    } catch (err) {
      setError(err.message || "Failed to load components.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const stats = useMemo(() => {
    const s = { total: components.length, healthy: 0, degraded: 0, unhealthy: 0, unknown: 0 };
    components.forEach((c) => { s[STATUS_BADGE[c.lastStatus] ? c.lastStatus : "unknown"] += 1; });
    return s;
  }, [components]);

  const openCreate = () => { setEditing(null); setSaveError(""); setModalOpen(true); };
  const openEdit = async (c) => {
    // Fetch the full record so webhook token is available for the URL display.
    try {
      const full = await getComponent(c.id);
      setEditing(full);
    } catch {
      setEditing(c);
    }
    setSaveError("");
    setModalOpen(true);
  };
  const closeModal = () => { setModalOpen(false); setEditing(null); setSaveError(""); };

  const handleSave = async (payload) => {
    setSaving(true);
    setSaveError("");
    try {
      if (editing?.id) {
        await updateComponent(editing.id, payload);
      } else {
        await createComponent(payload);
      }
      closeModal();
      await load();
    } catch (err) {
      setSaveError(err.message || "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (c) => {
    if (!window.confirm(`Delete component "${c.name}"? It will be unlinked from any topologies that use it.`)) return;
    try {
      await deleteComponent(c.id);
      await load();
    } catch (err) {
      setError(err.message || "Delete failed.");
    }
  };

  const handleCheck = async (c) => {
    setCheckingId(c.id);
    try {
      const updated = await checkComponentHealth(c.id);
      setComponents((prev) => prev.map((x) => (x.id === updated.id ? { ...x, ...updated } : x)));
    } catch (err) {
      setError(err.message || "Health check failed.");
    } finally {
      setCheckingId(null);
    }
  };

  if (!canView) return <AccessDeniedPage />;

  const filtered = components.filter((c) => {
    if (statusFilter !== "all" && (c.lastStatus || "unknown") !== statusFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return c.name.toLowerCase().includes(q) || (c.category || "").toLowerCase().includes(q) || (c.description || "").toLowerCase().includes(q);
    }
    return true;
  });

  return (
    <div className="ops-page">
      <PageTitle
        title="Components"
        subtitle="Reusable topology building blocks with health checks. Add them to any App Service topology."
        actionLabel={canCreate ? "New component" : undefined}
        onAction={canCreate ? openCreate : undefined}
      />

      {error && <p className="banner-message error" style={{ marginBottom: "1rem" }}>{error}</p>}

      <div className="user-filters" style={{ marginBottom: "1rem" }}>
        <input type="search" className="form-input" placeholder="Search by name, category…"
          value={search} onChange={(e) => setSearch(e.target.value)} style={{ maxWidth: 280 }} />
        <SearchableSelect className="form-select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="all">All statuses</option>
          <option value="healthy">Healthy</option>
          <option value="degraded">Degraded</option>
          <option value="unhealthy">Unhealthy</option>
          <option value="unknown">Unknown</option>
        </SearchableSelect>
        <span className="muted" style={{ fontSize: "0.82rem", alignSelf: "center" }}>
          {stats.total} total · {stats.healthy} healthy · {stats.unhealthy} unhealthy
        </span>
      </div>

      {loading ? (
        <LoadingState label="Loading components…" />
      ) : filtered.length === 0 ? (
        <EmptyState
          message="No components found."
          hint={components.length > 0 ? "Try adjusting the search or filter." : "Create your first reusable component to get started."}
        />
      ) : (
        <div className="table-shell">
          <div className="table-scroll-region">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Category</th>
                  <th>Description</th>
                  <th>Health check</th>
                  <th>Status</th>
                  <th>Last checked</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((c) => (
                  <tr key={c.id}>
                    <td><strong>{c.name}</strong></td>
                    <td className="muted" style={{ fontSize: "0.85rem" }}>{c.category || "—"}</td>
                    <td className="muted" style={{ fontSize: "0.82rem", maxWidth: 280 }}>{c.description || "—"}</td>
                    <td className="muted" style={{ fontSize: "0.82rem" }}>{CHECK_TYPE_LABEL[c.checkType] || c.checkType}</td>
                    <td><StatusBadge status={c.lastStatus} /></td>
                    <td className="muted" style={{ fontSize: "0.8rem" }}>{fmtDate(c.lastCheckedAt)}</td>
                    <td>
                      <div style={{ display: "flex", gap: "0.35rem" }}>
                        {canCheck && c.checkType !== "none" && (
                          <button className="btn-outline btn-compact" onClick={() => handleCheck(c)} disabled={checkingId === c.id}>
                            {checkingId === c.id ? "Checking…" : "Check"}
                          </button>
                        )}
                        {canUpdate && <button className="btn-outline btn-compact" onClick={() => openEdit(c)}>Edit</button>}
                        {canDelete && <button className="btn-outline btn-compact danger" onClick={() => handleDelete(c)}>Delete</button>}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {modalOpen && (
        <ComponentModal
          component={editing}
          onClose={closeModal}
          onSave={handleSave}
          saving={saving}
          error={saveError}
        />
      )}
    </div>
  );
}
