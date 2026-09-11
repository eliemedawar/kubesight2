import { DownIcon, PlusIcon, TrashIcon, UpIcon } from "./ciShared.jsx";

const TYPES = [
  ["text", "Text"],
  ["multiline", "Text block (a file)"],
  ["choice", "Choice"],
  ["boolean", "Yes / no"],
  ["dynamic_choice", "Choice from the repository"],
];

const SOURCES = [
  ["branches", "Branches"],
  ["tags", "Tags"],
  ["branches_and_tags", "Branches and tags"],
];

const blankParameter = () => ({
  name: "",
  type: "text",
  label: "",
  description: "",
  default: "",
  required: false,
  choices: [],
  source: "branches",
});

/**
 * Build parameters: what a person is asked before a build starts.
 *
 * Accepted values become environment variables for every stage, which is why a
 * name has to read as one — the editor says so rather than letting the save
 * fail. Choices for the repository type are resolved when the Run Build dialog
 * opens, so nothing is listed here.
 */
export default function BuildParameters({ parameters, canEdit, onChange }) {
  const items = parameters || [];

  const mutate = (index, patch) =>
    onChange(items.map((item, position) => (position === index ? { ...item, ...patch } : item)));

  const remove = (index) => onChange(items.filter((_, position) => position !== index));

  const move = (index, delta) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };

  return (
    <section className="form-section sg-ci-params">
      <div className="sg-ci-params-head">
        <div>
          <span className="sg-ci-inspector-kicker">Pipeline configuration</span>
          <h3>Build inputs</h3>
          <p>Values requested in the Run Build dialog and passed to every stage.</p>
        </div>
        {canEdit && (
          <button
            type="button"
            className="btn-outline btn-compact"
            onClick={() => onChange([...items, blankParameter()])}
          >
            <PlusIcon /> Add parameter
          </button>
        )}
      </div>

      <p className="muted sg-ci-params-note">
        Each accepted value becomes an environment variable with the same name.
        A container image stage also reads{" "}
        <code>IMAGE_NAME</code> and <code>IMAGE_TAG</code> from these.
      </p>

      {items.length === 0 ? (
        <p className="muted sg-ci-params-note">
          No parameters — Run Build asks only for the branch or tag.
        </p>
      ) : (
        <ol className="sg-ci-param-list">
          {items.map((param, index) => (
            <li key={index} className="sg-ci-param">
              <div className="sg-ci-param-head">
                <span className="sg-ci-stage-index">{index + 1}</span>
                <span>
                  <strong>{param.label || param.name || "New parameter"}</strong>
                  <small>{TYPES.find(([value]) => value === param.type)?.[1] || "Text"}</small>
                </span>
                {canEdit && (
                  <div className="sg-ci-param-actions">
                    <button
                      type="button"
                      className="icon-button"
                      aria-label="Move parameter up"
                      title="Move parameter up"
                      disabled={index === 0}
                      onClick={() => move(index, -1)}
                    >
                      <UpIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-button"
                      aria-label="Move parameter down"
                      title="Move parameter down"
                      disabled={index === items.length - 1}
                      onClick={() => move(index, 1)}
                    >
                      <DownIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-button danger"
                      aria-label="Remove parameter"
                      title="Remove parameter"
                      onClick={() => remove(index)}
                    >
                      <TrashIcon />
                    </button>
                  </div>
                )}
              </div>
              <div className="form-grid">
                <label>
                  Name *
                  <input
                    value={param.name || ""}
                    placeholder="DEPLOY_ENV"
                    disabled={!canEdit}
                    onChange={(event) => mutate(index, { name: event.target.value })}
                  />
                  <span className="field-hint">
                    Used as an environment variable: letters, digits and underscores.
                  </span>
                </label>
                <label>
                  Type
                  <select
                    value={param.type || "text"}
                    disabled={!canEdit}
                    onChange={(event) => mutate(index, { type: event.target.value })}
                  >
                    {TYPES.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>

                <label>
                  Label
                  <input
                    value={param.label || ""}
                    placeholder="(the name)"
                    disabled={!canEdit}
                    onChange={(event) => mutate(index, { label: event.target.value })}
                  />
                </label>
                <label>
                  Description
                  <input
                    value={param.description || ""}
                    placeholder="Shown under the field"
                    disabled={!canEdit}
                    onChange={(event) => mutate(index, { description: event.target.value })}
                  />
                </label>

                {param.type === "choice" && (
                  <label className="form-grid__full">
                    Options
                    <textarea
                      rows={3}
                      spellCheck={false}
                      style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
                      value={(param.choices || []).join("\n")}
                      placeholder={"uat\npreprod\nprod"}
                      disabled={!canEdit}
                      onChange={(event) =>
                        mutate(index, { choices: event.target.value.split("\n") })
                      }
                    />
                    <span className="field-hint">One per line.</span>
                  </label>
                )}

                {param.type === "dynamic_choice" && (
                  <label>
                    Listed from
                    <select
                      value={param.source || "branches"}
                      disabled={!canEdit}
                      onChange={(event) => mutate(index, { source: event.target.value })}
                    >
                      {SOURCES.map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                    <span className="field-hint">
                      Read from the repository each time Run Build opens.
                    </span>
                  </label>
                )}

                {param.type === "multiline" ? (
                  <label className="form-grid__full">
                    Default
                    <textarea
                      rows={8}
                      spellCheck={false}
                      style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
                      value={param.default || ""}
                      placeholder={"FROM registry.example.com/nginx\nCOPY ./dist/ /usr/share/nginx/html/"}
                      disabled={!canEdit}
                      onChange={(event) => mutate(index, { default: event.target.value })}
                    />
                    <span className="field-hint">
                      Written out whole, newlines and indentation intact — a Dockerfile, an
                      nginx server block, a .env. The build writes it to a file with a
                      command like <code>printf '%s' "$Dockerfile" &gt; Dockerfile</code>.
                    </span>
                  </label>
                ) : param.type === "boolean" ? (
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={String(param.default) === "true"}
                      disabled={!canEdit}
                      onChange={(event) =>
                        mutate(index, { default: event.target.checked ? "true" : "false" })
                      }
                    />
                    Default is yes
                  </label>
                ) : (
                  <label>
                    Default
                    <input
                      value={param.default || ""}
                      disabled={!canEdit}
                      onChange={(event) => mutate(index, { default: event.target.value })}
                    />
                  </label>
                )}

                {param.type !== "boolean" && (
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={Boolean(param.required)}
                      disabled={!canEdit}
                      onChange={(event) => mutate(index, { required: event.target.checked })}
                    />
                    Required
                  </label>
                )}
              </div>

            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
