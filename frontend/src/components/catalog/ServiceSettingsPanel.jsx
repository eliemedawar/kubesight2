import { useEffect, useRef, useState } from "react";
import {
  createCiSecret,
  deleteCiSecret,
  deleteCiService,
  listCiSecrets,
  updateCiSecret,
  updateCiService,
} from "../../api/ciApi.js";
import { listRegistries } from "../../api/registriesApi.js";
import {
  CRITICALITIES,
  PlusIcon,
  TrashIcon,
  applicationTypeLabel,
  formatRelative,
} from "./ciShared.jsx";

// What a build stage of this service may use. Each row is three-way: inherit the
// installation default, name a value, or take the limit off entirely. Ephemeral
// storage ships open — no limit, no request — so "Default" and "No limit" read
// the same out of the box, and the select still says which one was CHOSEN,
// because an installation that later sets a cap should apply to the first and
// not to the second.
const RESOURCE_FIELDS = [
  {
    key: "cpu",
    label: "CPU",
    placeholder: "2",
    hint: "Cores per stage container. 500m is half a core.",
  },
  {
    key: "memory",
    label: "Memory",
    placeholder: "4Gi",
    hint: "A stage that exceeds its memory limit is OOM-killed and the build fails there.",
  },
  {
    key: "ephemeralStorage",
    label: "Ephemeral storage",
    placeholder: "8Gi",
    hint:
      "Disk for the checkout, build output and the shared /workspace. With no " +
      "limit a build writes whatever the node has; with one, kubelet evicts the " +
      "pod at the ceiling and the build stops without saying why.",
  },
];

const OFF_WORDS = new Set(["off", "none", "no", "0", "false", "unlimited"]);

const resourceMode = (value) => {
  const text = String(value ?? "").trim();
  if (!text) return "default";
  return OFF_WORDS.has(text.toLowerCase()) ? "off" : "custom";
};

// "Default" has to name the number it resolves to, and the API reports that
// rather than the UI guessing: an installation that sets CI_STAGE_MEMORY_LIMIT
// should see its own value here.
const defaultLabel = (value) =>
  !value || OFF_WORDS.has(String(value).toLowerCase()) ? "no limit" : value;

/**
 * Settings tab: behaviour, build resources, registry, secrets, danger zone.
 *
 * The registry is where container_image stages push. Without one those stages
 * skip with an explanation rather than pretending to have built something, and
 * that explanation points here — so the field has to live here.
 *
 * Secret values are write-only. The list shows names and metadata because the
 * API has no path that returns a value — rotating means entering a new one.
 *
 * A secret is either scoped to this service or global — visible to every
 * service in the catalog. The scope is chosen when adding, and can be changed
 * afterwards, so a value that turns out to be shared (an NVD API key, a common
 * registry token) does not have to be pasted into each service in turn.
 */
export default function ServiceSettingsPanel({
  service,
  expectedSecrets = [],
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
    registryConnectionId: service.registryConnectionId || "",
    buildResources: { ...(service.buildResources || {}) },
  });
  // Which of the three the user picked, kept beside the value because the value
  // alone cannot say it: an empty box under "Custom" and an untouched field both
  // send nothing, and switching to Custom has to leave the box visible to type
  // in rather than snapping back to Default.
  const [resourceModes, setResourceModes] = useState(() =>
    Object.fromEntries(
      RESOURCE_FIELDS.map((field) => [
        field.key,
        resourceMode((service.buildResources || {})[field.key]),
      ])
    )
  );
  const resourceDefaults = service.buildResourceDefaults || {};
  const [registries, setRegistries] = useState([]);
  const [secrets, setSecrets] = useState([]);
  const [newSecret, setNewSecret] = useState({
    key: "",
    value: "",
    description: "",
    scope: "service",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const setResource = (key, value) =>
    setForm((prev) => {
      const next = { ...prev.buildResources };
      // Absent, not empty: an absent key is what tells the backend to fall back
      // to the installation default for this field only.
      if (value === null) delete next[key];
      else next[key] = value;
      return { ...prev, buildResources: next };
    });

  const setResourceMode = (key, mode) => {
    setResourceModes((prev) => ({ ...prev, [key]: mode }));
    if (mode === "default") setResource(key, null);
    else if (mode === "off") setResource(key, "off");
    else setResource(key, resourceMode(form.buildResources[key]) === "custom" ? form.buildResources[key] : "");
  };

  // Which of the application type's expected secrets are still missing.
  // Set-ness is recomputed from the list this panel already loaded rather than
  // read off the summary, so a chip flips the moment its secret is added
  // instead of on the next page load.
  const definedKeys = new Set(secrets.map((secret) => secret.key));
  const missingExpected = expectedSecrets.filter((item) => !definedKeys.has(item.key));
  // "Fill in" prefills the add form, which sits below the secrets table and can
  // be off-screen. Focusing the value takes the user there and puts the cursor
  // where the only thing still missing goes.
  const newSecretValueRef = useRef(null);

  const loadSecrets = () => {
    if (!canViewSecrets) return;
    listCiSecrets(service.id)
      .then((data) => setSecrets(data.items || []))
      .catch((err) => setError(err.message || "Could not load secrets."));
  };

  useEffect(loadSecrets, [service.id, canViewSecrets]);

  // Best effort: a user who cannot list registries still sees the field, with
  // whatever is already linked preserved on save.
  useEffect(() => {
    listRegistries()
      .then((data) => setRegistries(data.items || []))
      .catch(() => setRegistries([]));
  }, []);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await updateCiService(service.id, form);
      // What came back is the truth, and it can differ from what was typed:
      // "Custom" with an empty box stores nothing, which IS "Default". Re-derive
      // the rows from the response so the card never claims a setting the
      // service does not have.
      const saved = updated.buildResources || {};
      setForm((prev) => ({ ...prev, buildResources: { ...saved } }));
      setResourceModes(
        Object.fromEntries(
          RESOURCE_FIELDS.map((field) => [field.key, resourceMode(saved[field.key])])
        )
      );
      onSaved(updated);
    } catch (err) {
      setError(err.message || "Could not save settings.");
    } finally {
      setSaving(false);
    }
  };

  const addSecret = async () => {
    setError("");
    try {
      // A null service id is what picks the global route in the API client.
      await createCiSecret(newSecret.scope === "global" ? null : service.id, newSecret);
      setNewSecret({ key: "", value: "", description: "", scope: "service" });
      loadSecrets();
    } catch (err) {
      setError(err.message || "Could not add the secret.");
    }
  };

  // Moving a secret between scopes keeps the stored value, so pipelines that
  // already reference the name keep working — only who can see it changes.
  const changeScope = async (secret, scope) => {
    const message =
      scope === "global"
        ? `Make "${secret.key}" global? Every service in the catalog will be able ` +
          "to reference it, and its value stays as it is."
        : `Make "${secret.key}" service-only? Other services referencing it will ` +
          "lose it, and their next build fails on the reference.";
    if (!window.confirm(message)) return;
    setError("");
    try {
      await updateCiSecret(secret.id, { scope, serviceId: service.id });
      loadSecrets();
    } catch (err) {
      setError(err.message || "Could not change the scope.");
    }
  };

  const removeSecret = async (secret) => {
    const scopeNote =
      secret.scope === "global"
        ? "It is global: pipelines in every service that reference it will fail."
        : "Pipelines referencing it will fail.";
    if (!window.confirm(`Delete secret "${secret.key}"? ${scopeNote}`)) return;
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
            Image registry
            <select
              value={form.registryConnectionId || ""}
              disabled={!canEdit}
              onChange={(event) => set("registryConnectionId", event.target.value)}
            >
              <option value="">No registry — image stages skip</option>
              {registries.map((registry) => (
                <option key={registry.id} value={registry.id}>
                  {registry.name}
                  {registry.baseUrl ? ` — ${registry.baseUrl}` : ""}
                </option>
              ))}
            </select>
            <span className="field-hint">
              Where container_image stages push. The image is
              <code> &lt;registry&gt;/{service.slug}:&lt;tag&gt;</code> unless the stage sets
              IMAGE_NAME / IMAGE_TAG.
            </span>
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
      </section>

      <section className="form-section">
        <h4>Build resources</h4>
        <p className="muted">
          What every stage of this service's builds may use on the node. Leave a
          row on <strong>Default</strong> and the installation's value applies;
          choose <strong>No limit</strong> and the field is left off the pod
          entirely. A single stage can still override any of this from the
          pipeline editor.
        </p>
        <div className="form-grid">
          {RESOURCE_FIELDS.map((field) => {
            const mode = resourceModes[field.key];
            return (
              <label key={field.key}>
                {field.label}
                <div className="sg-ci-inline-field sg-ci-resource-row">
                  <select
                    value={mode}
                    disabled={!canEdit}
                    onChange={(event) => setResourceMode(field.key, event.target.value)}
                  >
                    <option value="default">
                      Default ({defaultLabel(resourceDefaults[field.key])})
                    </option>
                    <option value="custom">Custom…</option>
                    <option value="off">No limit</option>
                  </select>
                  {mode === "custom" && (
                    <input
                      value={form.buildResources[field.key] || ""}
                      placeholder={field.placeholder}
                      disabled={!canEdit}
                      aria-label={`${field.label} limit`}
                      onChange={(event) => setResource(field.key, event.target.value)}
                    />
                  )}
                </div>
                <span className="field-hint">{field.hint}</span>
              </label>
            );
          })}
        </div>
        {resourceModes.ephemeralStorage === "custom" && (
          <p className="field-hint">
            A limit here also raises the shared <code>/workspace</code> ceiling to
            match, so a build cannot be evicted below what you asked for, and a
            small request is added alongside it — without one Kubernetes would
            demand the full size free on every node before it would schedule the
            build.
          </p>
        )}
        {canEdit && (
          <div className="sg-ci-panel-actions">
            <button type="button" className="primary" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save settings"}
            </button>
            <span className="muted sg-ci-panel-note">
              Applies to the next build, including a retry of one that has
              already run.
            </span>
          </div>
        )}
      </section>

      {canViewSecrets && (
        <section className="form-section">
          <h4>Secrets</h4>
          <p className="muted">
            Referenced by name from a pipeline stage and injected as environment
            variables. Values are encrypted at rest, never returned by the API, and
            masked out of build logs. A <strong>global</strong> secret is available
            to every service; one of this service's own with the same name wins over
            it.
          </p>

          {expectedSecrets.length > 0 && (
            <div className="sg-ci-expected">
              <p className="muted">
                {/* Pluralised rather than "a <type> build", because the
                    article would be wrong for Android and iOS. */}
                {missingExpected.length === 0
                  ? `Every secret ${applicationTypeLabel(service.applicationType)} builds usually need is set.`
                  : `${applicationTypeLabel(service.applicationType)} builds usually need these. They are suggestions, not requirements — a build is never blocked by a missing one.`}
              </p>
              <ul className="sg-ci-expected-list">
                {expectedSecrets.map((item) => {
                  const isSet = definedKeys.has(item.key);
                  return (
                    <li key={item.key} className={isSet ? "is-set" : ""}>
                      <code>{item.key}</code>
                      <span className={`chip ${isSet ? "is-ok" : "is-warn"}`}>
                        {isSet ? "set" : "not set"}
                      </span>
                      <span className="muted">{item.description}</span>
                      {canManageSecrets && !isSet && (
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() => {
                            setNewSecret((prev) => ({
                              ...prev,
                              key: item.key,
                              value: "",
                              description: item.description,
                            }));
                            newSecretValueRef.current?.focus();
                            newSecretValueRef.current?.scrollIntoView({
                              block: "center",
                              behavior: "smooth",
                            });
                          }}
                        >
                          Fill in
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

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
                        <span
                          className={`chip sg-ci-scope-chip${
                            secret.scope === "global" ? " is-global" : ""
                          }`}
                        >
                          {secret.scope === "global" ? "global" : "this service"}
                        </span>
                      </td>
                      <td>{secret.description || "—"}</td>
                      <td>{secret.lastUsedAt ? formatRelative(secret.lastUsedAt) : "never"}</td>
                      <td className="table-actions-cell">
                        {canManageSecrets && (
                          <span className="sg-ci-secret-row-actions">
                            <button
                              type="button"
                              className="btn-link"
                              onClick={() =>
                                changeScope(
                                  secret,
                                  secret.scope === "global" ? "service" : "global"
                                )
                              }
                            >
                              {secret.scope === "global" ? "Make service-only" : "Make global"}
                            </button>
                            <button
                              type="button"
                              className="icon-button danger"
                              aria-label={`Delete ${secret.key}`}
                              onClick={() => removeSecret(secret)}
                            >
                              <TrashIcon />
                            </button>
                          </span>
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
                ref={newSecretValueRef}
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
              {/* Scope is chosen before the value is sent, because the two
                  scopes are different API routes — not a flag on one record. */}
              <select
                className="sg-ci-secret-scope"
                aria-label="Secret scope"
                value={newSecret.scope}
                onChange={(event) =>
                  setNewSecret((prev) => ({ ...prev, scope: event.target.value }))
                }
              >
                <option value="service">This service only</option>
                <option value="global">Global — all services</option>
              </select>
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
