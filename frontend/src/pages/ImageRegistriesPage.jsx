import { useCallback, useEffect, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import SearchableSelect from "../components/common/SearchableSelect.jsx";
import {
  checkImage,
  createRegistry,
  deleteRegistry,
  listRegistries,
  testRegistry,
  updateRegistry,
} from "../api/registriesApi.js";

const EMPTY_FORM = {
  name: "",
  registryType: "nexus",
  baseUrl: "",
  imageHosts: "",
  authMode: "basic",
  username: "",
  password: "",
  verifyTls: true,
  enforcement: "block",
  enabled: true,
};

const ENFORCEMENT_LABELS = {
  block: "Block deploy if image is missing",
  warn: "Warn only (allow deploy)",
  off: "Off (do not check)",
};

const STATUS_BADGE = {
  found: { label: "Available", className: "status-ok" },
  not_found: { label: "Not found", className: "status-error" },
  unreachable: { label: "Unreachable", className: "status-warn" },
  no_connection: { label: "No linked registry", className: "status-muted" },
};

export default function ImageRegistriesPage({ canManage = false }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [form, setForm] = useState(EMPTY_FORM);
  const [editingId, setEditingId] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState(null);

  const [probeImage, setProbeImage] = useState("");
  const [probeResult, setProbeResult] = useState(null);
  const [probing, setProbing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await listRegistries();
      setItems(data.items || []);
    } catch (err) {
      setError(err.message || "Failed to load registries.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const setField = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const closeForm = () => {
    setShowForm(false);
    setEditingId(null);
    setForm(EMPTY_FORM);
  };

  const openCreate = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setError("");
    setNotice("");
    setShowForm(true);
  };

  const startEdit = (row) => {
    setEditingId(row.id);
    setForm({
      name: row.name || "",
      registryType: row.registryType || "nexus",
      baseUrl: row.baseUrl || "",
      imageHosts: (row.imageHosts || []).join(", "),
      authMode: row.authMode || "basic",
      username: row.username || "",
      password: "", // never prefilled; blank keeps the stored secret
      verifyTls: row.verifyTls !== false,
      enforcement: row.enforcement || "block",
      enabled: row.enabled !== false,
    });
    setError("");
    setNotice("");
    setShowForm(true);
  };

  const submit = async (event) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const payload = {
        ...form,
        imageHosts: form.imageHosts
          .split(/[\s,;]+/)
          .map((h) => h.trim())
          .filter(Boolean),
      };
      if (editingId && !payload.password) {
        delete payload.password; // keep existing secret when left blank on edit
      }
      if (editingId) {
        await updateRegistry(editingId, payload);
        setNotice("Registry updated.");
      } else {
        await createRegistry(payload);
        setNotice("Registry linked.");
      }
      closeForm();
      await load();
    } catch (err) {
      setError(err.message || "Failed to save the registry.");
    } finally {
      setSaving(false);
    }
  };

  const runTest = async (id) => {
    setTestingId(id);
    setError("");
    setNotice("");
    try {
      // The request succeeds (200) even when the registry is unreachable — the
      // real verdict is in result.status, so route failures to the error banner
      // rather than the green success banner.
      const result = await testRegistry(id);
      if (result.status === "ok") {
        setNotice(result.message || "Connection successful.");
      } else {
        setError(result.message || "Connection test failed.");
      }
      await load();
    } catch (err) {
      setError(err.message || "Connection test failed.");
    } finally {
      setTestingId(null);
    }
  };

  const remove = async (row) => {
    if (!window.confirm(`Remove linked registry "${row.name}"?`)) {
      return;
    }
    setError("");
    setNotice("");
    try {
      await deleteRegistry(row.id);
      if (editingId === row.id) {
        closeForm();
      }
      await load();
    } catch (err) {
      setError(err.message || "Failed to remove the registry.");
    }
  };

  const runProbe = async () => {
    const image = probeImage.trim();
    if (!image) {
      return;
    }
    setProbing(true);
    setProbeResult(null);
    try {
      setProbeResult(await checkImage(image));
    } catch (err) {
      setError(err.message || "Image check failed.");
    } finally {
      setProbing(false);
    }
  };

  return (
    <div className="registry-page">
      <PageTitle
        title="Image Registries"
        subtitle="Link a container registry (e.g. Sonatype Nexus) so KubeSight verifies each image exists before deploying."
        actionLabel={canManage ? "Add a registry" : undefined}
        onAction={canManage ? openCreate : undefined}
      />

      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      {notice ? (
        <div className="banner banner-success" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      <section className="card">
        <h3>Linked registries</h3>
        {loading ? (
          <p className="muted">Loading…</p>
        ) : items.length === 0 ? (
          <EmptyState message="No registries linked yet. Use “Add a registry” to enable pre-deploy image checks." />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Registry host</th>
                <th>Auth</th>
                <th>Enforcement</th>
                <th>Enabled</th>
                <th>Last test</th>
                {canManage ? <th aria-label="Actions" /> : null}
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const aliasHosts = (row.imageHosts || []).filter(
                  (h) => h !== row.host,
                );
                return (
                  <tr key={row.id}>
                    <td>{row.name}</td>
                    <td>
                      <span className="mono">{row.host || row.baseUrl}</span>
                      {aliasHosts.length ? (
                        <div className="muted registry-alias">
                          matches: {aliasHosts.join(", ")}
                        </div>
                      ) : null}
                    </td>
                    <td>{row.authMode === "basic" ? `Basic (${row.username})` : "Anonymous"}</td>
                    <td>{ENFORCEMENT_LABELS[row.enforcement] || row.enforcement}</td>
                    <td>{row.enabled ? "Yes" : "No"}</td>
                    <td>
                      {row.lastTestStatus ? (
                        <span
                          className={`badge ${row.lastTestStatus === "ok" ? "status-ok" : "status-error"}`}
                          title={row.lastTestMessage || ""}
                        >
                          {row.lastTestStatus === "ok" ? "OK" : "Failed"}
                        </span>
                      ) : (
                        <span className="muted">Never</span>
                      )}
                    </td>
                    {canManage ? (
                      <td className="actions">
                        <button type="button" className="link-button" onClick={() => runTest(row.id)} disabled={testingId === row.id}>
                          {testingId === row.id ? "Testing…" : "Test"}
                        </button>
                        <button type="button" className="link-button" onClick={() => startEdit(row)}>
                          Edit
                        </button>
                        <button type="button" className="link-button danger" onClick={() => remove(row)}>
                          Remove
                        </button>
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h3>Check an image</h3>
        <p className="muted">
          Test whether a specific image reference is available in a linked registry — the same check runs automatically before every deploy.
        </p>
        <div className="inline-form">
          <input
            value={probeImage}
            onChange={(e) => setProbeImage(e.target.value)}
            placeholder="registry.example.com/team/api:v1.2.3"
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                runProbe();
              }
            }}
          />
          <button type="button" onClick={runProbe} disabled={probing || !probeImage.trim()}>
            {probing ? "Checking…" : "Check"}
          </button>
        </div>
        {probeResult ? (
          <p className={`probe-result ${STATUS_BADGE[probeResult.status]?.className || ""}`}>
            <span className="badge">{STATUS_BADGE[probeResult.status]?.label || probeResult.status}</span>{" "}
            {probeResult.message}
          </p>
        ) : null}
      </section>

      {showForm && canManage ? (
        <div className="modal-overlay" role="presentation" onClick={closeForm}>
          <section
            className="card modal-panel registry-modal"
            role="dialog"
            aria-labelledby="registry-form-title"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="modal-header">
              <div>
                <h3 id="registry-form-title">{editingId ? "Edit registry" : "Add a registry"}</h3>
                <p className="muted">
                  Connect by URL or IP; declare the host(s) as they appear in your image references.
                </p>
              </div>
              <button type="button" className="modal-close" onClick={closeForm} aria-label="Close">
                <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
                </svg>
              </button>
            </header>

            <form className="settings-form registry-form" onSubmit={submit}>
              <label>
                Name
                <input value={form.name} onChange={(e) => setField("name", e.target.value)} placeholder="Nexus (prod)" required />
              </label>
              <label>
                Registry URL / host (how KubeSight connects)
                <input
                  value={form.baseUrl}
                  onChange={(e) => setField("baseUrl", e.target.value)}
                  placeholder="nexus.example.com:8083 or 10.0.0.5:8083"
                  required
                />
              </label>
              <label className="registry-form__row-full">
                Image host(s) — as written in image references
                <input
                  value={form.imageHosts}
                  onChange={(e) => setField("imageHosts", e.target.value)}
                  placeholder="registry.example.com (comma-separated; defaults to the URL host)"
                />
                <span className="field-hint">
                  Set this when you connect by IP but pull images by hostname, so images like
                  {" "}<code>registry.example.com/app:tag</code> are matched to this registry.
                </span>
              </label>
              <label>
                Type
                <SearchableSelect value={form.registryType} onChange={(e) => setField("registryType", e.target.value)}>
                  <option value="nexus">Sonatype Nexus</option>
                  <option value="generic">Generic Docker V2</option>
                </SearchableSelect>
              </label>
              <label>
                Authentication
                <SearchableSelect value={form.authMode} onChange={(e) => setField("authMode", e.target.value)}>
                  <option value="basic">Basic (username / password)</option>
                  <option value="none">Anonymous</option>
                </SearchableSelect>
              </label>
              {form.authMode === "basic" ? (
                <>
                  <label>
                    Username
                    <input value={form.username} onChange={(e) => setField("username", e.target.value)} autoComplete="off" />
                  </label>
                  <label>
                    Password
                    <input
                      type="password"
                      value={form.password}
                      onChange={(e) => setField("password", e.target.value)}
                      placeholder={editingId ? "Leave blank to keep current" : ""}
                      autoComplete="new-password"
                    />
                  </label>
                </>
              ) : null}
              <label>
                When an image is missing
                <SearchableSelect value={form.enforcement} onChange={(e) => setField("enforcement", e.target.value)}>
                  <option value="block">Block the deploy</option>
                  <option value="warn">Warn only</option>
                  <option value="off">Do not check</option>
                </SearchableSelect>
              </label>
              <div className="registry-form__checks">
                <label className="checkbox-label">
                  <input type="checkbox" checked={form.verifyTls} onChange={(e) => setField("verifyTls", e.target.checked)} />
                  Verify TLS certificate
                </label>
                <label className="checkbox-label">
                  <input type="checkbox" checked={form.enabled} onChange={(e) => setField("enabled", e.target.checked)} />
                  Enabled
                </label>
              </div>
              <div className="modal-actions registry-form__row-full">
                <button type="button" className="secondary" onClick={closeForm}>
                  Cancel
                </button>
                <button type="submit" className="primary" disabled={saving}>
                  {saving ? "Saving…" : editingId ? "Save changes" : "Add registry"}
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}
    </div>
  );
}
