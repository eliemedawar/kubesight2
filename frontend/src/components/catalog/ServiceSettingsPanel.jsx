import { useEffect, useState } from "react";
import {
  createCiSecret,
  deleteCiSecret,
  deleteCiService,
  listCiSecrets,
  updateCiService,
} from "../../api/ciApi.js";
import { CRITICALITIES, PlusIcon, TrashIcon, formatRelative } from "./ciShared.jsx";

/**
 * Settings tab: behaviour, secrets, danger zone.
 *
 * Secret values are write-only. The list shows names and metadata because the
 * API has no path that returns a value — rotating means entering a new one.
 */
export default function ServiceSettingsPanel({
  service,
  onSaved,
  onDeleted,
  canEdit,
  canDelete,
  canViewSecrets,
  canManageSecrets,
}) {
  const [form, setForm] = useState({
    status: service.status,
    criticality: service.criticality || "medium",
    ownerTeam: service.ownerTeam || "",
    maxConcurrentBuilds: service.maxConcurrentBuilds || 1,
  });
  const [secrets, setSecrets] = useState([]);
  const [newSecret, setNewSecret] = useState({ key: "", value: "", description: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const loadSecrets = () => {
    if (!canViewSecrets) return;
    listCiSecrets(service.id)
      .then((data) => setSecrets(data.items || []))
      .catch((err) => setError(err.message || "Could not load secrets."));
  };

  useEffect(loadSecrets, [service.id, canViewSecrets]);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      onSaved(await updateCiService(service.id, form));
    } catch (err) {
      setError(err.message || "Could not save settings.");
    } finally {
      setSaving(false);
    }
  };

  const addSecret = async () => {
    setError("");
    try {
      await createCiSecret(service.id, newSecret);
      setNewSecret({ key: "", value: "", description: "" });
      loadSecrets();
    } catch (err) {
      setError(err.message || "Could not add the secret.");
    }
  };

  const removeSecret = async (secret) => {
    if (!window.confirm(`Delete secret "${secret.key}"? Pipelines referencing it will fail.`))
      return;
    try {
      await deleteCiSecret(secret.id);
      loadSecrets();
    } catch (err) {
      setError(err.message || "Could not delete the secret.");
    }
  };

  const removeService = async () => {
    if (
      !window.confirm(
        `Delete "${service.name}"? Its pipelines, builds, logs, and artifact records ` +
          "are removed with it. This cannot be undone."
      )
    )
      return;
    try {
      await deleteCiService(service.id);
      onDeleted();
    } catch (err) {
      setError(err.message || "Could not delete the service.");
    }
  };

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      <section className="form-section">
        <h4>Behaviour</h4>
        <div className="form-grid">
          <label>
            Status
            <select
              value={form.status}
              disabled={!canEdit}
              onChange={(event) => set("status", event.target.value)}
            >
              <option value="active">Active</option>
              <option value="paused">Paused</option>
              <option value="archived">Archived</option>
            </select>
            <span className="field-hint">Only an active service accepts builds.</span>
          </label>
          <label>
            Criticality
            <select
              value={form.criticality}
              disabled={!canEdit}
              onChange={(event) => set("criticality", event.target.value)}
            >
              {CRITICALITIES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label>
            Owner / team
            <input
              value={form.ownerTeam}
              disabled={!canEdit}
              onChange={(event) => set("ownerTeam", event.target.value)}
            />
          </label>
          <label>
            Max concurrent builds
            <input
              type="number"
              min={1}
              max={20}
              value={form.maxConcurrentBuilds}
              disabled={!canEdit}
              onChange={(event) => set("maxConcurrentBuilds", event.target.value)}
            />
            <span className="field-hint">
              Further builds queue instead of running in parallel.
            </span>
          </label>
        </div>
        {canEdit && (
          <div className="sg-ci-panel-actions">
            <button type="button" className="primary" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save settings"}
            </button>
          </div>
        )}
      </section>

      {canViewSecrets && (
        <section className="form-section">
          <h4>Secrets</h4>
          <p className="muted">
            Referenced by name from a pipeline stage and injected as environment
            variables. Values are encrypted at rest, never returned by the API, and
            masked out of build logs.
          </p>

          {secrets.length === 0 ? (
            <p className="muted">No secrets defined.</p>
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Scope</th>
                    <th>Description</th>
                    <th>Last used</th>
                    <th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {secrets.map((secret) => (
                    <tr key={secret.id}>
                      <td>
                        <code>{secret.key}</code>
                      </td>
                      <td>
                        <span className="chip">{secret.scope}</span>
                      </td>
                      <td>{secret.description || "—"}</td>
                      <td>{secret.lastUsedAt ? formatRelative(secret.lastUsedAt) : "never"}</td>
                      <td className="table-actions-cell">
                        {canManageSecrets && secret.scope === "service" && (
                          <button
                            type="button"
                            className="icon-button danger"
                            aria-label={`Delete ${secret.key}`}
                            onClick={() => removeSecret(secret)}
                          >
                            <TrashIcon />
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {canManageSecrets && (
            <div className="sg-ci-secret-form">
              <input
                placeholder="SECRET_NAME"
                value={newSecret.key}
                onChange={(event) =>
                  setNewSecret((prev) => ({ ...prev, key: event.target.value }))
                }
              />
              {/* A textarea, not a password input: the useful secrets here are
                  whole files — a gradle.properties, a Dockerfile, a PEM — and a
                  single-line input silently eats the newlines. Values stay
                  write-only; nothing displays one again after it is saved. */}
              <textarea
                className="sg-ci-secret-value"
                placeholder="Value — paste a whole file if that is what it is"
                rows={2}
                spellCheck={false}
                autoComplete="off"
                value={newSecret.value}
                onChange={(event) =>
                  setNewSecret((prev) => ({ ...prev, value: event.target.value }))
                }
              />
              <input
                placeholder="Description (optional)"
                value={newSecret.description}
                onChange={(event) =>
                  setNewSecret((prev) => ({ ...prev, description: event.target.value }))
                }
              />
              <button
                type="button"
                className="btn-outline btn-compact"
                onClick={addSecret}
                disabled={!newSecret.key.trim() || !newSecret.value}
              >
                <PlusIcon /> Add
              </button>
            </div>
          )}
        </section>
      )}

      {canDelete && (
        <section className="form-section sg-ci-danger">
          <h4>Danger zone</h4>
          <p className="muted">
            Deleting removes the service, its pipelines, builds, logs, and artifact
            records. Artifact files already stored are left in place.
          </p>
          <button type="button" className="btn-outline danger" onClick={removeService}>
            Delete this service
          </button>
        </section>
      )}
    </div>
  );
}
