import { useCallback, useEffect, useState } from "react";
import {
  createCustomCluster,
  deleteCustomCluster,
  listCustomClusters,
  testCustomCluster,
  updateCustomCluster,
} from "../api";
import { formatAccessError } from "../utils/authz.js";
import SearchableSelect from "../components/common/SearchableSelect.jsx";

const emptyForm = () => ({
  connectionMethod: "kubeconfig",
  authenticationType: "token",
  name: "",
  contextName: "",
  kubeconfigContent: "",
  host: "",
  port: "6443",
  protocol: "https",
  bearerToken: "",
  caCertificate: "",
  clientCertificate: "",
  clientKey: "",
  showAdvanced: false,
  contextOverride: "",
  skipTlsVerify: false,
  customCa: "",
  connectionTimeout: "",
});

function formatDate(value) {
  if (!value) {
    return "-";
  }
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function statusLabel(cluster) {
  const status = cluster.lastConnectionStatus;
  if (status === "connected") {
    return "Connected";
  }
  if (status === "error") {
    return "Error";
  }
  return "Not tested";
}

function connectionMethodLabel(method) {
  if (method === "manual") {
    return "Manual";
  }
  return "Kubeconfig";
}

function validateForm(form, mode) {
  if (!form.name.trim()) {
    return "Cluster name is required.";
  }
  if (form.connectionMethod === "kubeconfig") {
    if (mode === "create" && !form.kubeconfigContent.trim()) {
      return "Paste kubeconfig YAML or upload a kubeconfig file.";
    }
    return null;
  }
  if (!form.host.trim()) {
    return "Host is required.";
  }
  if (!form.port) {
    return "Port is required.";
  }
  if (!form.authenticationType) {
    return "Authentication type is required.";
  }
  if (form.authenticationType === "token" && mode === "create" && !form.bearerToken.trim()) {
    return "Bearer token is required.";
  }
  if (
    form.authenticationType === "certificate" &&
    mode === "create" &&
    (!form.clientCertificate.trim() || !form.clientKey.trim())
  ) {
    return "Client certificate and client key are required.";
  }
  return null;
}

function buildPayload(form, mode) {
  const advanced = {
    contextOverride: form.contextOverride.trim() || undefined,
    skipTlsVerify: form.skipTlsVerify,
    customCa: form.customCa.trim() || undefined,
    connectionTimeout: form.connectionTimeout ? Number(form.connectionTimeout) : undefined,
  };

  const payload = {
    name: form.name.trim(),
    connectionMethod: form.connectionMethod,
    contextName: form.contextName.trim() || form.contextOverride.trim() || undefined,
    skipTlsVerify: form.skipTlsVerify,
    connectionTimeoutSeconds: form.connectionTimeout ? Number(form.connectionTimeout) : undefined,
    advanced,
  };

  if (form.connectionMethod === "kubeconfig") {
    if (form.kubeconfigContent.trim()) {
      payload.kubeconfigContent = form.kubeconfigContent;
    }
    return payload;
  }

  payload.host = form.host.trim();
  payload.port = Number(form.port);
  payload.protocol = form.protocol;
  payload.authenticationType = form.authenticationType;
  if (form.bearerToken.trim()) {
    payload.bearerToken = form.bearerToken;
  }
  if (form.caCertificate.trim()) {
    payload.caCertificate = form.caCertificate;
  }
  if (form.clientCertificate.trim()) {
    payload.clientCertificate = form.clientCertificate;
  }
  if (form.clientKey.trim()) {
    payload.clientKey = form.clientKey;
  }
  if (mode === "edit" && form.authenticationType === "anonymous") {
    payload.authenticationType = "anonymous";
  }
  return payload;
}

function ClusterModal({ open, mode, initial, onClose, onSave, saving, error }) {
  const [form, setForm] = useState(emptyForm());

  useEffect(() => {
    if (open) {
      setForm({
        ...emptyForm(),
        connectionMethod: initial?.connectionMethod || "kubeconfig",
        authenticationType: initial?.authenticationType || "token",
        name: initial?.name || "",
        host: initial?.host || "",
        port: String(initial?.port ?? "6443"),
        protocol: initial?.protocol || "https",
        contextName: initial?.contextName || "",
        contextOverride: initial?.contextName || "",
        skipTlsVerify: Boolean(initial?.skipTlsVerify),
        connectionTimeout: initial?.connectionTimeoutSeconds
          ? String(initial.connectionTimeoutSeconds)
          : "",
        kubeconfigContent: "",
        bearerToken: "",
        clientCertificate: "",
        clientKey: "",
        caCertificate: "",
      });
    }
  }, [open, initial]);

  if (!open) {
    return null;
  }

  const handleFile = async (event) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    const text = await file.text();
    setForm((prev) => ({ ...prev, kubeconfigContent: text }));
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    const validationError = validateForm(form, mode);
    if (validationError) {
      return;
    }
    await onSave(buildPayload(form, mode));
  };

  const isKubeconfig = form.connectionMethod === "kubeconfig";
  const isManual = !isKubeconfig;
  const localError = validateForm(form, mode);

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel cluster-modal"
        role="dialog"
        aria-labelledby="cluster-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3 id="cluster-modal-title">{mode === "create" ? "Add Cluster" : "Edit Cluster"}</h3>
            <p className="muted">
              Credentials are stored only on the server as a kubeconfig file and are never returned to
              the browser.
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg>
          </button>
        </header>

        <form className="cluster-form" onSubmit={handleSubmit}>
          <fieldset className="cluster-connection-method">
            <legend>Connection Method</legend>
            <label className="cluster-radio">
              <input
                type="radio"
                name="connectionMethod"
                value="kubeconfig"
                checked={isKubeconfig}
                disabled={mode === "edit"}
                onChange={() => setForm((prev) => ({ ...prev, connectionMethod: "kubeconfig" }))}
              />
              <span>
                Kubeconfig <span className="recommended-badge">Recommended</span>
              </span>
            </label>
            <label className="cluster-radio">
              <input
                type="radio"
                name="connectionMethod"
                value="manual"
                checked={isManual}
                disabled={mode === "edit"}
                onChange={() => setForm((prev) => ({ ...prev, connectionMethod: "manual" }))}
              />
              <span>Manual Connection</span>
            </label>
            <p className="cluster-helper muted">
              Most production Kubernetes clusters provide access through kubeconfig. Manual connection
              is intended for advanced users.
            </p>
          </fieldset>

          <label>
            Cluster Name
            <input
              required
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
            />
          </label>

          {isKubeconfig ? (
            <>
              <label>
                Context Name (optional)
                <input
                  value={form.contextName}
                  onChange={(event) => setForm((prev) => ({ ...prev, contextName: event.target.value }))}
                  placeholder="Leave empty to use kubeconfig current-context"
                />
              </label>
              <label>
                Paste Kubeconfig YAML
                <textarea
                  required={mode === "create"}
                  rows={8}
                  value={form.kubeconfigContent}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, kubeconfigContent: event.target.value }))
                  }
                  placeholder="Paste kubeconfig YAML here"
                />
              </label>
              <label className="file-upload-label">
                Upload Kubeconfig File
                <input type="file" accept=".yaml,.yml,.conf,text/*" onChange={handleFile} />
              </label>
            </>
          ) : (
            <>
              <label>
                Host / DNS
                <input
                  required
                  value={form.host}
                  onChange={(event) => setForm((prev) => ({ ...prev, host: event.target.value }))}
                />
              </label>
              <div className="cluster-form-row">
                <label>
                  Port
                  <input
                    required
                    type="number"
                    min={1}
                    max={65535}
                    value={form.port}
                    onChange={(event) => setForm((prev) => ({ ...prev, port: event.target.value }))}
                  />
                </label>
                <label>
                  Protocol
                  <SearchableSelect
                    value={form.protocol}
                    onChange={(event) => setForm((prev) => ({ ...prev, protocol: event.target.value }))}
                  >
                    <option value="https">https</option>
                    <option value="http">http</option>
                  </SearchableSelect>
                </label>
              </div>

              <fieldset className="cluster-auth-type">
                <legend>Authentication Type</legend>
                {["token", "certificate", "anonymous"].map((authType) => (
                  <label key={authType} className="cluster-radio">
                    <input
                      type="radio"
                      name="authenticationType"
                      value={authType}
                      checked={form.authenticationType === authType}
                      onChange={() => setForm((prev) => ({ ...prev, authenticationType: authType }))}
                    />
                    <span>
                      {authType === "token"
                        ? "Bearer Token"
                        : authType === "certificate"
                          ? "Client Certificate"
                          : "Anonymous"}
                    </span>
                  </label>
                ))}
              </fieldset>

              {form.authenticationType === "token" ? (
                <>
                  <label>
                    Bearer Token
                    <input
                      type="password"
                      autoComplete="off"
                      required={mode === "create"}
                      value={form.bearerToken}
                      onChange={(event) =>
                        setForm((prev) => ({ ...prev, bearerToken: event.target.value }))
                      }
                      placeholder={mode === "edit" ? "Leave blank to keep existing token" : ""}
                    />
                  </label>
                  <label>
                    CA Certificate (optional)
                    <textarea
                      rows={4}
                      value={form.caCertificate}
                      onChange={(event) =>
                        setForm((prev) => ({ ...prev, caCertificate: event.target.value }))
                      }
                    />
                  </label>
                </>
              ) : null}

              {form.authenticationType === "certificate" ? (
                <>
                  <label>
                    Client Certificate
                    <textarea
                      required={mode === "create"}
                      rows={4}
                      value={form.clientCertificate}
                      onChange={(event) =>
                        setForm((prev) => ({ ...prev, clientCertificate: event.target.value }))
                      }
                      placeholder={mode === "edit" ? "Leave blank to keep existing certificate" : ""}
                    />
                  </label>
                  <label>
                    Client Key
                    <textarea
                      required={mode === "create"}
                      rows={4}
                      value={form.clientKey}
                      onChange={(event) => setForm((prev) => ({ ...prev, clientKey: event.target.value }))}
                      placeholder={mode === "edit" ? "Leave blank to keep existing key" : ""}
                    />
                  </label>
                  <label>
                    CA Certificate (optional)
                    <textarea
                      rows={4}
                      value={form.caCertificate}
                      onChange={(event) =>
                        setForm((prev) => ({ ...prev, caCertificate: event.target.value }))
                      }
                    />
                  </label>
                </>
              ) : null}

              {form.authenticationType === "anonymous" ? (
                <p className="banner-message warn cluster-anonymous-warning">
                  Anonymous Kubernetes access is rarely enabled in production and may fail.
                </p>
              ) : null}

              <label>
                Context Name (optional)
                <input
                  value={form.contextName}
                  onChange={(event) => setForm((prev) => ({ ...prev, contextName: event.target.value }))}
                />
              </label>
            </>
          )}

          <div className="cluster-advanced">
            <button
              type="button"
              className="btn-outline cluster-advanced-toggle"
              aria-expanded={form.showAdvanced}
              onClick={() => setForm((prev) => ({ ...prev, showAdvanced: !prev.showAdvanced }))}
            >
              {form.showAdvanced ? "▼" : "▶"} Advanced Settings
            </button>
            {form.showAdvanced ? (
              <div className="cluster-advanced-body">
                <label>
                  Context Override
                  <input
                    value={form.contextOverride}
                    onChange={(event) =>
                      setForm((prev) => ({ ...prev, contextOverride: event.target.value }))
                    }
                  />
                </label>
                <label className="cluster-checkbox">
                  <input
                    type="checkbox"
                    checked={form.skipTlsVerify}
                    onChange={(event) =>
                      setForm((prev) => ({ ...prev, skipTlsVerify: event.target.checked }))
                    }
                  />
                  Skip TLS Verify
                </label>
                <label>
                  Custom CA
                  <textarea
                    rows={3}
                    value={form.customCa}
                    onChange={(event) => setForm((prev) => ({ ...prev, customCa: event.target.value }))}
                  />
                </label>
                <label>
                  Connection Timeout (seconds)
                  <input
                    type="number"
                    min={1}
                    max={300}
                    value={form.connectionTimeout}
                    onChange={(event) =>
                      setForm((prev) => ({ ...prev, connectionTimeout: event.target.value }))
                    }
                  />
                </label>
              </div>
            ) : null}
          </div>

          {localError ? <p className="routing-error">{localError}</p> : null}
          {error ? <p className="routing-error">{error}</p> : null}
          <footer className="modal-actions">
            <button type="button" onClick={onClose} disabled={saving}>
              Cancel
            </button>
            <button type="submit" disabled={saving || Boolean(localError)}>
              {saving ? "Saving..." : mode === "create" ? "Add Cluster" : "Save Changes"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function DataTable({ columns, rows }) {
  const renderCell = (colKey, value, row) => {
    if (colKey === "actions") {
      return row.actions;
    }
    if (value == null) {
      return "-";
    }
    const text = String(value);
    if (colKey !== "status") {
      return text;
    }
    const toneMap = {
      connected: "ok",
      error: "danger",
      "not tested": "warn",
    };
    const tone = toneMap[text.toLowerCase()] || "info";
    return <span className={`status-pill ${tone}`}>{text}</span>;
  };

  return (
    <div className="table-shell">
      <table>
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.key}>{col.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.publicId}>
              {columns.map((col) => (
                <td key={col.key}>{renderCell(col.key, row[col.key], row)}</td>
              ))}
            </tr>
          ))}
          {!rows.length ? (
            <tr>
              <td colSpan={columns.length} className="muted">
                No custom clusters configured yet.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

export default function ClusterManagementPage({
  onClustersChanged,
  canAdd = true,
  canUpdate = true,
  canRemove = true,
  canTest = true,
}) {
  const [clusters, setClusters] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState("create");
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [modalError, setModalError] = useState("");
  const [testingId, setTestingId] = useState("");

  const loadClusters = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await listCustomClusters();
      setClusters(result.items || []);
    } catch (loadError) {
      setError(loadError.message);
      setClusters([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadClusters();
  }, [loadClusters]);

  const openCreate = () => {
    setModalMode("create");
    setEditing(null);
    setModalError("");
    setModalOpen(true);
  };

  const openEdit = (cluster) => {
    setModalMode("edit");
    setEditing(cluster);
    setModalError("");
    setModalOpen(true);
  };

  const handleSave = async (payload) => {
    setSaving(true);
    setModalError("");
    try {
      if (modalMode === "create") {
        const result = await createCustomCluster(payload);
        const test = result.test || {};
        if (test.success && test.reachable) {
          setMessage(`Cluster added. Connection OK (${test.serverVersion || "unknown"}).`);
        } else {
          setMessage(`Cluster saved but connection test failed: ${test.error || "unknown error"}`);
        }
      } else if (editing) {
        await updateCustomCluster(editing.publicId, payload);
        setMessage("Cluster updated.");
      }
      setModalOpen(false);
      await loadClusters();
      if (onClustersChanged) {
        await onClustersChanged();
      }
    } catch (saveError) {
      setModalError(saveError.message);
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async (cluster) => {
    setTestingId(cluster.publicId);
    setMessage("");
    setError("");
    try {
      const result = await testCustomCluster(cluster.publicId);
      if (result.success && result.reachable) {
        setMessage(
          `Connection OK for ${cluster.name}: ${result.serverVersion || "unknown"} · ${result.latencyMs ?? "-"} ms`
        );
      } else {
        setMessage(`Connection failed for ${cluster.name}: ${result.error || "unknown error"}`);
      }
      await loadClusters();
      if (onClustersChanged) {
        await onClustersChanged();
      }
    } catch (testError) {
      setError(testError.message);
    } finally {
      setTestingId("");
    }
  };

  const handleRemove = async (cluster) => {
    if (!window.confirm(`Remove cluster "${cluster.name}"?`)) {
      return;
    }
    setError("");
    try {
      await deleteCustomCluster(cluster.publicId);
      setMessage(`Removed ${cluster.name}.`);
      await loadClusters();
      if (onClustersChanged) {
        await onClustersChanged();
      }
    } catch (removeError) {
      setError(removeError.message);
    }
  };

  const columns = [
    { key: "name", label: "Name" },
    { key: "connection", label: "Connection" },
    { key: "host", label: "Host" },
    { key: "port", label: "Port" },
    { key: "protocol", label: "Protocol" },
    { key: "status", label: "Status" },
    { key: "lastTestedAt", label: "Last Tested" },
    { key: "actions", label: "Actions" },
  ];

  const rows = clusters.map((cluster) => ({
    ...cluster,
    connection: connectionMethodLabel(cluster.connectionMethod),
    status: statusLabel(cluster),
    lastTestedAt: formatDate(cluster.lastTestedAt),
    actions: (
      <div className="table-actions">
        {canUpdate ? (
          <button type="button" className="btn-outline" onClick={() => openEdit(cluster)}>
            Edit
          </button>
        ) : null}
        {canTest ? (
          <button
            type="button"
            className="btn-outline"
            onClick={() => handleTest(cluster)}
            disabled={testingId === cluster.publicId}
          >
            {testingId === cluster.publicId ? "Testing..." : "Test Connection"}
          </button>
        ) : null}
        {canRemove ? (
          <button type="button" className="btn-outline danger-outline" onClick={() => handleRemove(cluster)}>
            Remove
          </button>
        ) : null}
      </div>
    ),
  }));

  return (
    <>
      <header className="page-title cluster-mgmt-header">
        <div>
          <h2>Cluster Management</h2>
          <p>
            Connect clusters using kubeconfig (recommended) or manual API settings. All paths store a
            server-side kubeconfig for kubectl operations.
          </p>
        </div>
        {canAdd ? (
          <button type="button" className="primary" onClick={openCreate}>
            Add Cluster
          </button>
        ) : null}
      </header>

      {loading ? <p className="muted">Loading clusters...</p> : null}
      {formatAccessError(error) ? <p className="banner-message error">{formatAccessError(error)}</p> : null}
      {message ? <p className="banner-message">{message}</p> : null}

      <DataTable columns={columns} rows={rows} />

      <ClusterModal
        open={modalOpen}
        mode={modalMode}
        initial={editing}
        onClose={() => setModalOpen(false)}
        onSave={handleSave}
        saving={saving}
        error={modalError}
      />
    </>
  );
}
