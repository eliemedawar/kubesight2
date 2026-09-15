import { useEffect, useState } from "react";
import { listRegistries } from "../../api/registriesApi.js";

/**
 * The only questions left: what Hermes could not safely know.
 *
 * Three kinds, and the difference matters because each becomes something
 * different in KubeSight:
 *
 *   parameter  a build input, visible and overridable in Run Build
 *   secret     an encrypted, write-only value a stage references by name
 *   registry   a link to a registry connection — KubeSight keeps registry
 *              credentials there, so asking for a username and password here
 *              would store two values nothing would ever read
 *
 * A secret field is write-only by construction: it starts empty, it is typed
 * once, and no route can read it back afterwards. A secret the service already
 * has is not asked for again.
 */

const KIND_NOTE = {
  secret: "Stored encrypted. Masked out of every build log, and never readable again.",
  parameter: "Becomes a build input — you can change it per build in Run build.",
  registry: "KubeSight keeps registry credentials on the connection, not on the pipeline.",
};

export default function RequiredConfigForm({ items = [], values, onChange, disabled = false }) {
  const [registries, setRegistries] = useState([]);
  const [revealed, setRevealed] = useState({});

  const needsRegistry = items.some((item) => item.kind === "registry");

  useEffect(() => {
    if (!needsRegistry) return undefined;
    let live = true;
    listRegistries()
      .then((data) => {
        if (live) setRegistries((data.items || []).filter((item) => item.enabled !== false));
      })
      .catch(() => {
        /* The field falls back to a plain id box rather than blocking. */
      });
    return () => {
      live = false;
    };
  }, [needsRegistry]);

  if (!items.length) {
    return (
      <p className="sg-ci-required-none">
        Nothing else is needed — Hermes could determine everything this pipeline
        requires.
      </p>
    );
  }

  const set = (name, value) => onChange({ ...values, [name]: value });
  const outstanding = items.filter(
    (item) => item.required !== false && !String(values[item.name] || "").trim()
  );

  return (
    <section className="sg-ci-required" aria-label="Required configuration">
      <header>
        <h4>Required configuration</h4>
        <p className="muted">
          {outstanding.length
            ? `${outstanding.length} ${
                outstanding.length === 1 ? "value needs" : "values need"
              } your input.`
            : "Everything needed has a value."}
        </p>
      </header>

      <div className="form-grid">
        {items.map((item) => {
          const value = values[item.name] ?? "";
          const missing = item.required !== false && !String(value).trim();
          return (
            <label key={item.name} className="form-grid__full">
              <span className="sg-ci-required-label">
                {item.label || item.name}
                {item.required !== false && <em aria-hidden="true"> *</em>}
                <code>{item.name}</code>
              </span>

              {item.kind === "registry" ? (
                registries.length ? (
                  <select
                    value={value}
                    disabled={disabled}
                    onChange={(event) => set(item.name, event.target.value)}
                  >
                    <option value="">Select a registry connection…</option>
                    {registries.map((registry) => (
                      <option key={registry.id} value={registry.id}>
                        {registry.name} — {registry.baseUrl}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={value}
                    disabled={disabled}
                    placeholder="Registry connection id"
                    onChange={(event) => set(item.name, event.target.value)}
                  />
                )
              ) : item.kind === "secret" ? (
                <span className="sg-ci-required-secret">
                  <input
                    type={revealed[item.name] ? "text" : "password"}
                    value={value}
                    disabled={disabled}
                    autoComplete="new-password"
                    placeholder="••••••••••••"
                    onChange={(event) => set(item.name, event.target.value)}
                  />
                  <button
                    type="button"
                    className="btn-outline btn-compact"
                    onClick={() =>
                      setRevealed((prev) => ({ ...prev, [item.name]: !prev[item.name] }))
                    }
                  >
                    {revealed[item.name] ? "Hide" : "Show"}
                  </button>
                </span>
              ) : (
                <input
                  value={value}
                  disabled={disabled}
                  placeholder={item.example || ""}
                  onChange={(event) => set(item.name, event.target.value)}
                />
              )}

              <span className="field-hint">
                {item.description || KIND_NOTE[item.kind]}
                {/* Why it is being asked for, not just that it is. A reason
                    quoted from the repository is what turns a form into an
                    explanation. */}
                {item.reason && <em className="sg-ci-required-reason"> {item.reason}</em>}
                {item.usedByStages?.length > 0 && (
                  <span className="sg-ci-required-usedby">
                    {" "}
                    Used by {item.usedByStages.join(", ")}.
                  </span>
                )}
              </span>
              {missing && <span className="sg-ci-required-missing">Still needed</span>}
            </label>
          );
        })}
      </div>
    </section>
  );
}
