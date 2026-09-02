import { useEffect, useState } from "react";
import {
  createCiSourceCredential,
  listCiBranches,
  listCiSourceCredentials,
  testCiSource,
  updateCiSource,
} from "../../api/ciApi.js";

/**
 * Source tab: which repository, which branch, which credential.
 *
 * Credentials are picked, never typed here — the secret itself is entered once
 * in the "Add credential" form and is never read back. That is why the picker
 * shows a profile name and type and nothing else.
 */
export default function SourcePanel({ service, onSaved, canEdit, canManageSecrets }) {
  const [form, setForm] = useState({
    repositoryUrl: service.repositoryUrl || "",
    defaultBranch: service.defaultBranch || "main",
    workingDirectory: service.workingDirectory || "",
    credentialProfileId: service.credentialProfileId || "",
  });
  const [credentials, setCredentials] = useState([]);
  const [branches, setBranches] = useState([]);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [error, setError] = useState("");
  const [addingCredential, setAddingCredential] = useState(false);

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const loadCredentials = async () => {
    try {
      const data = await listCiSourceCredentials();
      setCredentials(data.items || []);
    } catch (err) {
      setError(err.message || "Could not load credential profiles.");
    }
  };

  useEffect(() => {
    loadCredentials();
  }, []);

  // Branch options come from the live repository, so the picker can only ever
  // offer refs that actually exist.
  useEffect(() => {
    if (!service.sourceConfigured) return;
    let cancelled = false;
    listCiBranches(service.id)
      .then((data) => {
        if (!cancelled) setBranches(data.items || []);
      })
      .catch(() => {
        /* Non-fatal: the field stays a free-text input. */
      });
    return () => {
      cancelled = true;
    };
  }, [service.id, service.sourceConfigured, service.repositoryUrl]);

  const save = async () => {
    setSaving(true);
    setError("");
    setTestResult(null);
    try {
      const updated = await updateCiSource(service.id, form);
      onSaved(updated);
    } catch (err) {
      setError(err.message || "Could not save the source configuration.");
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await testCiSource(service.id));
    } catch (err) {
      setTestResult({ ok: false, message: err.message || "The connection test failed." });
    } finally {
      setTesting(false);
    }
  };

  const branchOptions = branches.filter((item) => item.type === "branch");

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      <section className="form-section">
        <h4>Repository</h4>
        <div className="form-grid">
          <label className="form-grid__full">
            Repository URL *
            <input
              value={form.repositoryUrl}
              placeholder="https://bitbucket.org/workspace/repository"
              disabled={!canEdit}
              onChange={(event) => set("repositoryUrl", event.target.value)}
            />
            <span className="field-hint">
              Bitbucket Cloud over HTTPS. Credentials must not be part of the URL.
            </span>
          </label>

          <label>
            Default branch
            {branchOptions.length > 0 ? (
              <select
                value={form.defaultBranch}
                disabled={!canEdit}
                onChange={(event) => set("defaultBranch", event.target.value)}
              >
                {!branchOptions.some((item) => item.value === form.defaultBranch) && (
                  <option value={form.defaultBranch}>{form.defaultBranch}</option>
                )}
                {branchOptions.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.value}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={form.defaultBranch}
                disabled={!canEdit}
                onChange={(event) => set("defaultBranch", event.target.value)}
              />
            )}
          </label>

          <label>
            Working directory
            <input
              value={form.workingDirectory}
              placeholder="services/payment"
              disabled={!canEdit}
              onChange={(event) => set("workingDirectory", event.target.value)}
            />
            <span className="field-hint">
              Optional. For monorepos — stages run relative to this path.
            </span>
          </label>

          <label className="form-grid__full">
            Credential profile *
            <div className="sg-ci-inline-field">
              <select
                value={form.credentialProfileId}
                disabled={!canEdit}
                onChange={(event) => set("credentialProfileId", event.target.value)}
              >
                <option value="">Select a credential…</option>
                {credentials.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.credentialType})
                  </option>
                ))}
              </select>
              {canManageSecrets && (
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  onClick={() => setAddingCredential(true)}
                >
                  Add credential
                </button>
              )}
            </div>
            <span className="field-hint">
              Stored encrypted and shared across services — the secret is never shown again.
            </span>
          </label>
        </div>

        {canEdit && (
          <div className="sg-ci-panel-actions">
            <button
              type="button"
              className="primary"
              disabled={saving || !form.repositoryUrl.trim() || !form.credentialProfileId}
              onClick={save}
            >
              {saving ? "Saving…" : "Save source"}
            </button>
            {service.sourceConfigured && (
              <button
                type="button"
                className="btn-outline"
                disabled={testing}
                onClick={runTest}
              >
                {testing ? "Testing…" : "Test connection"}
              </button>
            )}
          </div>
        )}

        {testResult && (
          <p className={`banner-message ${testResult.ok ? "success" : "error"}`}>
            {testResult.message}
          </p>
        )}
      </section>

      {service.sourceConfigured && (
        <section className="form-section">
          <h4>Resolved</h4>
          <dl className="sg-ci-dl">
            <div>
              <dt>Provider</dt>
              <dd>{service.repositoryProvider}</dd>
            </div>
            <div>
              <dt>Workspace</dt>
              <dd>{service.repositoryWorkspace}</dd>
            </div>
            <div>
              <dt>Repository</dt>
              <dd>{service.repositoryName}</dd>
            </div>
            <div>
              <dt>Credential</dt>
              <dd>{service.credentialProfileName || "—"}</dd>
            </div>
          </dl>
        </section>
      )}

      {addingCredential && (
        <CredentialModal
          onClose={() => setAddingCredential(false)}
          onCreated={async (created) => {
            setAddingCredential(false);
            await loadCredentials();
            set("credentialProfileId", created.id);
          }}
        />
      )}
    </div>
  );
}

function CredentialModal({ onClose, onCreated }) {
  const [form, setForm] = useState({
    name: "",
    credentialType: "repository_access_token",
    principal: "",
    secret: "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const submit = async () => {
    setSaving(true);
    setError("");
    try {
      onCreated(await createCiSourceCredential(form));
    } catch (err) {
      setError(err.message || "Could not save the credential.");
      setSaving(false);
    }
  };

  const needsPrincipal = form.credentialType === "api_token";

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card"
        role="dialog"
        aria-label="Add source credential"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>Add a source credential</h3>
          <p className="muted">
            Encrypted at rest and never displayed again. Read access is all CI needs.
          </p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <div className="form-grid">
          <label className="form-grid__full">
            Name *
            <input
              value={form.name}
              maxLength={120}
              placeholder="e.g. bitbucket-ci-read"
              onChange={(event) => set("name", event.target.value)}
            />
          </label>
          <label>
            Type
            <select
              value={form.credentialType}
              onChange={(event) => set("credentialType", event.target.value)}
            >
              <option value="repository_access_token">Repository access token</option>
              <option value="api_token">Atlassian API token</option>
              <option value="oauth">OAuth token</option>
            </select>
          </label>
          {needsPrincipal && (
            <label>
              Atlassian account email *
              <input
                value={form.principal}
                onChange={(event) => set("principal", event.target.value)}
              />
            </label>
          )}
          <label className="form-grid__full">
            Secret *
            <input
              type="password"
              value={form.secret}
              autoComplete="new-password"
              onChange={(event) => set("secret", event.target.value)}
            />
          </label>
        </div>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={
              saving ||
              !form.name.trim() ||
              !form.secret ||
              (needsPrincipal && !form.principal.trim())
            }
            onClick={submit}
          >
            {saving ? "Saving…" : "Add credential"}
          </button>
        </div>
      </div>
    </div>
  );
}
