import { useEffect, useRef, useState } from "react";
import { createCiSecret, deleteCiSecret, listCiSecrets, updateCiSecret } from "../../../api/ciApi.js";
import { applicationTypeLabel, formatRelative } from "../ciShared.jsx";
import { Field, Segmented } from "../pipeline/controls.jsx";
import { PlIcon } from "../pipeline/icons.jsx";

// What the backend accepts (services/ci/secrets.py _clean_key).
const NAME_RE = /^[A-Za-z0-9_.-]+$/;
// What a shell can read as $NAME — the variable a stage gets unless it maps
// the secret to another name.
const ENV_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

const blank = { key: "", value: "", description: "", scope: "service" };

/**
 * Secrets: names and metadata in a list, values write-only.
 *
 * The API has no path that returns a value, so "edit" means replacing it —
 * the row offers exactly that, rather than a field that looks readable and
 * is not. Suggested secrets for the application type sit on top as
 * one-click starts, and a secret the pipeline references but nobody defined
 * is the one thing that turns red.
 */
export default function SecretsSection({ service, expectedSecrets, canManage, onError, onNotice, onCount }) {
  const [secrets, setSecrets] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [adding, setAdding] = useState(null);
  const [replacing, setReplacing] = useState(null);
  const [replaceValue, setReplaceValue] = useState("");
  const [busy, setBusy] = useState(false);
  const valueRef = useRef(null);

  const load = () =>
    listCiSecrets(service.id)
      .then((data) => {
        setSecrets(data.items || []);
        onCount?.(data.items || []);
      })
      .catch((err) => onError(err.message || "Could not load secrets."))
      .finally(() => setLoaded(true));

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [service.id]);

  useEffect(() => {
    if (adding && adding.key && !adding.value) valueRef.current?.focus();
  }, [adding?.key]); // eslint-disable-line react-hooks/exhaustive-deps

  const defined = new Set(secrets.map((secret) => secret.key));
  const own = secrets.filter((secret) => secret.scope !== "global");
  const global = secrets.filter((secret) => secret.scope === "global");
  const shadowed = new Set(
    own.filter((secret) => global.some((item) => item.key === secret.key)).map((secret) => secret.key)
  );
  const missing = expectedSecrets.filter((item) => !defined.has(item.key));

  const nameError =
    adding && adding.key && !NAME_RE.test(adding.key.trim())
      ? "Letters, digits, '_', '-' and '.' only."
      : adding && adding.key && secrets.some((s) => s.key === adding.key.trim() && s.scope === adding.scope)
        ? `A ${adding.scope === "global" ? "global" : "service"} secret with this name already exists — replace its value instead.`
        : "";
  const envWarning =
    adding && adding.key && !nameError && !ENV_RE.test(adding.key.trim())
      ? "Not usable as $NAME in a shell — map it to another variable name on the stage."
      : "";

  const add = async () => {
    setBusy(true);
    try {
      // A null service id is what picks the global route in the API client.
      await createCiSecret(adding.scope === "global" ? null : service.id, {
        ...adding,
        key: adding.key.trim(),
      });
      onNotice(`Secret ${adding.key.trim()} added${adding.scope === "global" ? " for every service" : ""}.`);
      setAdding(null);
      load();
    } catch (err) {
      onError(err.message || "Could not add the secret.");
    } finally {
      setBusy(false);
    }
  };

  const replace = async (secret) => {
    setBusy(true);
    try {
      await updateCiSecret(secret.id, { value: replaceValue });
      onNotice(`New value saved for ${secret.key}. The next build uses it.`);
      setReplacing(null);
      setReplaceValue("");
      load();
    } catch (err) {
      onError(err.message || "Could not replace the value.");
    } finally {
      setBusy(false);
    }
  };

  // Moving a secret between scopes keeps the stored value, so pipelines that
  // already reference the name keep working — only who can see it changes.
  const changeScope = async (secret) => {
    const scope = secret.scope === "global" ? "service" : "global";
    const message =
      scope === "global"
        ? `Make "${secret.key}" global? Every service in the catalog will be able to reference it, and its value stays as it is.`
        : `Make "${secret.key}" this service's only? Other services referencing it lose it, and their next build fails on the reference.`;
    if (!window.confirm(message)) return;
    try {
      await updateCiSecret(secret.id, { scope, serviceId: service.id });
      load();
    } catch (err) {
      onError(err.message || "Could not change the scope.");
    }
  };

  const remove = async (secret) => {
    const note =
      secret.scope === "global"
        ? "It is global: pipelines in every service that reference it will fail."
        : "Pipelines referencing it will fail.";
    if (!window.confirm(`Delete secret "${secret.key}"? ${note}`)) return;
    try {
      await deleteCiSecret(secret.id);
      onNotice(`Secret ${secret.key} deleted.`);
      load();
    } catch (err) {
      onError(err.message || "Could not delete the secret.");
    }
  };

  const row = (secret) => {
    const isGlobal = secret.scope === "global";
    const hidden = isGlobal && shadowed.has(secret.key);
    return (
      <li key={secret.id} className={`st-secret${hidden ? " is-shadowed" : ""}`}>
        <span className="st-secret-icon" aria-hidden="true">
          <PlIcon name="key" />
        </span>
        <div className="st-secret-copy">
          <strong>
            <code>{secret.key}</code>
            <span className={`pl-tag${isGlobal ? " is-info" : ""}`}>{isGlobal ? "Global" : "This service"}</span>
            {hidden && <span className="pl-tag">Overridden by this service's own</span>}
          </strong>
          <small>
            {secret.description ? `${secret.description} · ` : ""}
            {secret.lastUsedAt ? `used ${formatRelative(secret.lastUsedAt)}` : "not used by a build yet"}
            {secret.updatedAt && ` · set ${formatRelative(secret.updatedAt)}`}
          </small>
          {replacing === secret.id && (
            <div className="st-replace">
              <textarea
                className="is-mono"
                rows={3}
                spellCheck={false}
                autoComplete="off"
                placeholder="The new value — it replaces the old one for every stage that uses this secret"
                value={replaceValue}
                onChange={(event) => setReplaceValue(event.target.value)}
                autoFocus
              />
              <div className="st-replace-actions">
                <button
                  type="button"
                  className="btn-outline btn-compact"
                  onClick={() => {
                    setReplacing(null);
                    setReplaceValue("");
                  }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="primary btn-compact"
                  disabled={busy || !replaceValue}
                  onClick={() => replace(secret)}
                >
                  <PlIcon name="check" /> Save new value
                </button>
              </div>
            </div>
          )}
        </div>
        {canManage && replacing !== secret.id && (
          <div className="st-secret-actions">
            <button type="button" className="btn-ghost st-action" onClick={() => setReplacing(secret.id)}>
              <PlIcon name="refresh" /> Replace value
            </button>
            <button type="button" className="btn-ghost st-action" onClick={() => changeScope(secret)}>
              <PlIcon name="link" /> {isGlobal ? "Make service-only" : "Make global"}
            </button>
            <button
              type="button"
              className="btn-ghost pl-tool is-danger"
              aria-label={`Delete ${secret.key}`}
              title="Delete"
              onClick={() => remove(secret)}
            >
              <PlIcon name="trash" />
            </button>
          </div>
        )}
      </li>
    );
  };

  return (
    <div className="st-secrets">
      {expectedSecrets.length > 0 && (
        <div className={`st-suggested${missing.length ? "" : " is-done"}`}>
          <PlIcon name={missing.length ? "sparkle" : "check"} />
          <div>
            <strong>
              {missing.length
                ? `${applicationTypeLabel(service.applicationType)} builds usually use ${missing.length === 1 ? "this" : "these"}`
                : `Every secret ${applicationTypeLabel(service.applicationType)} builds usually use is set`}
            </strong>
            <p>Suggestions, not requirements — a build is never blocked by a missing one.</p>
            <ul>
              {expectedSecrets.map((item) => {
                const isSet = defined.has(item.key);
                return (
                  <li key={item.key} className={isSet ? "is-set" : ""}>
                    <code>{item.key}</code>
                    <span>{item.description}</span>
                    {isSet ? (
                      <span className="st-set">
                        <PlIcon name="check" /> Set
                      </span>
                    ) : (
                      canManage && (
                        <button
                          type="button"
                          className="btn-outline btn-compact"
                          onClick={() => setAdding({ ...blank, key: item.key, description: item.description })}
                        >
                          <PlIcon name="plus" /> Add
                        </button>
                      )
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}

      {adding && (
        <div className="st-add" role="group" aria-label="New secret">
          <div className="pl-grid">
            <Field
              label="Name"
              htmlFor="st-secret-name"
              error={nameError}
              hint={envWarning || "What a pipeline stage references, and the variable it becomes."}
            >
              <input
                id="st-secret-name"
                className="is-mono"
                value={adding.key}
                placeholder="NEXUS_TOKEN"
                spellCheck={false}
                autoComplete="off"
                onChange={(event) => setAdding((prev) => ({ ...prev, key: event.target.value }))}
                autoFocus={!adding.key}
              />
            </Field>
            <Field label="Available to">
              <Segmented
                label="Secret scope"
                value={adding.scope}
                options={[
                  { value: "service", label: "This service" },
                  { value: "global", label: "Every service" },
                ]}
                onChange={(scope) => setAdding((prev) => ({ ...prev, scope }))}
              />
            </Field>
            {/* A textarea, not a password input: the useful secrets here are
                whole files — a gradle.properties, a PEM, an .npmrc — and a
                single-line input silently eats the newlines. */}
            <Field
              label="Value"
              htmlFor="st-secret-value"
              wide
              hint="Encrypted at rest, never shown again, and masked out of build logs. Paste a whole file if that is what it is."
            >
              <textarea
                id="st-secret-value"
                ref={valueRef}
                className="is-mono"
                rows={4}
                spellCheck={false}
                autoComplete="off"
                value={adding.value}
                onChange={(event) => setAdding((prev) => ({ ...prev, value: event.target.value }))}
              />
            </Field>
            <Field label="Description" htmlFor="st-secret-desc" optional wide>
              <input
                id="st-secret-desc"
                value={adding.description}
                placeholder="What it is and who owns it"
                onChange={(event) => setAdding((prev) => ({ ...prev, description: event.target.value }))}
              />
            </Field>
          </div>
          <div className="st-add-actions">
            <button type="button" className="btn-outline btn-compact" onClick={() => setAdding(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary btn-compact"
              disabled={busy || !adding.key.trim() || !adding.value || Boolean(nameError)}
              onClick={add}
            >
              <PlIcon name="lock" /> {busy ? "Saving…" : "Save secret"}
            </button>
          </div>
        </div>
      )}

      {!loaded ? null : secrets.length === 0 && !adding ? (
        <div className="pl-empty">
          <span className="pl-empty-glyph" aria-hidden="true">
            <PlIcon name="key" />
          </span>
          <strong>No secrets yet</strong>
          <p>Tokens, keys and whole config files a build needs, attached to stages by name on the Pipeline tab.</p>
          {canManage && (
            <button type="button" className="primary btn-compact" onClick={() => setAdding({ ...blank })}>
              <PlIcon name="plus" /> Add a secret
            </button>
          )}
        </div>
      ) : (
        <>
          {own.length > 0 && (
            <div className="st-secret-group">
              <h5 className="pl-block-title">This service · {own.length}</h5>
              <ul className="st-secret-list">{own.map(row)}</ul>
            </div>
          )}
          {global.length > 0 && (
            <div className="st-secret-group">
              <h5 className="pl-block-title">Global — shared by every service · {global.length}</h5>
              <ul className="st-secret-list">{global.map(row)}</ul>
            </div>
          )}
          {canManage && !adding && (
            <button type="button" className="btn-ghost pl-rows-add" onClick={() => setAdding({ ...blank })}>
              <PlIcon name="plus" /> Add a secret
            </button>
          )}
        </>
      )}
    </div>
  );
}
