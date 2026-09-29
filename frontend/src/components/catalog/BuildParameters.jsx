import { useEffect, useId, useState } from "react";
import { Field, Segmented, Switch } from "./pipeline/controls.jsx";
import { PlIcon } from "./pipeline/icons.jsx";
import {
  MAX_PARAMETERS,
  moveItem,
  parameterProblems,
  stagesUsingParameter,
} from "./pipeline/stageModel.js";

const TYPES = [
  { value: "text", label: "Text", hint: "One line" },
  { value: "choice", label: "Choice", hint: "Pick from a list" },
  { value: "boolean", label: "Yes / no", hint: "A checkbox" },
  { value: "dynamic_choice", label: "Branch or tag", hint: "Listed from the repo" },
  { value: "multiline", label: "Text block", hint: "A whole file" },
];

const SOURCES = [
  { value: "branches", label: "Branches" },
  { value: "tags", label: "Tags" },
  { value: "branches_and_tags", label: "Both" },
];

const typeLabel = (type) => TYPES.find((item) => item.value === type)?.label || "Text";

const blankParameter = (parameters) => {
  let name = "NEW_INPUT";
  for (let suffix = 2; parameters.some((item) => item.name === name); suffix += 1) {
    name = `NEW_INPUT_${suffix}`;
  }
  return {
    name,
    type: "text",
    label: "",
    description: "",
    default: "",
    required: false,
    choices: [],
    source: "branches",
  };
};

/**
 * Build inputs: what a person is asked before a build starts.
 *
 * Accepted values become environment variables for every stage, which is why
 * a name has to read as one — the editor says so as it is typed rather than
 * letting the save fail. The preview on the right is the Run Build dialog as
 * it will look, so an input is judged by the person who has to answer it.
 */
export default function BuildParameters({ parameters, stages = [], canEdit, onChange, onOpenStage }) {
  const items = parameters || [];
  const [open, setOpen] = useState(items.length ? 0 : null);

  useEffect(() => {
    if (open !== null && open >= items.length) setOpen(items.length ? items.length - 1 : null);
  }, [items.length, open]);

  const mutate = (index, patch) =>
    onChange(items.map((item, position) => (position === index ? { ...item, ...patch } : item)));

  const remove = (index) => {
    const used = stagesUsingParameter(items[index].name, stages);
    if (
      used.length &&
      !window.confirm(
        `${used.map(({ stage }) => `“${stage.name}”`).join(", ")} ${
          used.length === 1 ? "runs" : "run"
        } only when this input matches. Remove it anyway? Those stages will keep a condition on a name that no longer exists.`
      )
    ) {
      return;
    }
    onChange(items.filter((_, position) => position !== index));
    setOpen(null);
  };

  const move = (index, delta) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    onChange(moveItem(items, index, target));
    setOpen((current) => (current === index ? target : current === target ? index : current));
  };

  const add = () => {
    onChange([...items, blankParameter(items)]);
    setOpen(items.length);
  };

  return (
    <div className="pl-inputs">
      <div className="pl-inputs-main">
        <header className="pl-view-head">
          <div>
            <span className="pl-kicker">Asked in the Run Build dialog</span>
            <h3>Build inputs</h3>
            <p>
              Each answer becomes an environment variable with the same name in every stage, and
              can switch stages on or off with a run condition.
            </p>
          </div>
          {canEdit && items.length > 0 && (
            <button
              type="button"
              className="btn-outline btn-compact"
              onClick={add}
              disabled={items.length >= MAX_PARAMETERS}
            >
              <PlIcon name="plus" /> Add input
            </button>
          )}
        </header>

        {items.length === 0 ? (
          <div className="pl-empty">
            <span className="pl-empty-glyph" aria-hidden="true">
              <PlIcon name="inputs" />
            </span>
            <strong>No build inputs</strong>
            <p>
              Run Build asks only for the branch or tag. Add an input to ask for an environment,
              a version, or a yes/no like “Deploy after build?”.
            </p>
            {canEdit && (
              <button type="button" className="primary btn-compact" onClick={add}>
                <PlIcon name="plus" /> Add the first input
              </button>
            )}
          </div>
        ) : (
          <ol className="pl-input-list">
            {items.map((param, index) => (
              <InputCard
                key={index}
                param={param}
                index={index}
                total={items.length}
                problems={parameterProblems(param, index, items)}
                usedBy={stagesUsingParameter(param.name, stages)}
                open={open === index}
                canEdit={canEdit}
                onToggle={() => setOpen((current) => (current === index ? null : index))}
                onChange={(patch) => mutate(index, patch)}
                onMove={(delta) => move(index, delta)}
                onRemove={() => remove(index)}
                onOpenStage={onOpenStage}
              />
            ))}
          </ol>
        )}

        <p className="pl-field-hint">
          A container image stage also reads <code>IMAGE_NAME</code> and <code>IMAGE_TAG</code> from
          inputs of those names, and they win over the stage's own values.
        </p>
      </div>

      <RunPreview parameters={items} />
    </div>
  );
}

function InputCard({
  param,
  index,
  total,
  problems,
  usedBy,
  open,
  canEdit,
  onToggle,
  onChange,
  onMove,
  onRemove,
  onOpenStage,
}) {
  const ids = useId();
  const problemFor = (field) => problems.find((item) => item.field === field)?.message;
  const choices = param.choices || [];

  return (
    <li className={`pl-input${open ? " is-open" : ""}${problems.length ? " has-problems" : ""}`}>
      <div className="pl-input-head">
        <button
          type="button"
          className="btn-ghost pl-input-toggle"
          aria-expanded={open}
          aria-controls={`${ids}-body`}
          onClick={onToggle}
        >
          <span className="pl-input-index">{index + 1}</span>
          <span className="pl-input-copy">
            <strong>{param.label || param.name || "New input"}</strong>
            <small>
              <code>{param.name || "—"}</code>
              <span className="pl-tag">{typeLabel(param.type)}</span>
              {param.required && param.type !== "boolean" && <span className="pl-tag">Required</span>}
              {usedBy.length > 0 && (
                <span className="pl-tag is-info">
                  <PlIcon name="branch" /> Steers {usedBy.length} stage{usedBy.length === 1 ? "" : "s"}
                </span>
              )}
              {problems.length > 0 && (
                <span className="pl-tag is-error">
                  <PlIcon name="alert" /> {problems.length} to fix
                </span>
              )}
            </small>
          </span>
          <PlIcon name="chevron" className="pl-setting-chevron" />
        </button>
        {canEdit && (
          <div className="pl-toolgroup" role="group" aria-label={`Actions for ${param.name || "input"}`}>
            <button type="button" className="btn-ghost pl-tool" aria-label="Move input up" title="Move up" disabled={index === 0} onClick={() => onMove(-1)}>
              <PlIcon name="up" />
            </button>
            <button type="button" className="btn-ghost pl-tool" aria-label="Move input down" title="Move down" disabled={index === total - 1} onClick={() => onMove(1)}>
              <PlIcon name="down" />
            </button>
            <button type="button" className="btn-ghost pl-tool is-danger" aria-label="Remove input" title="Remove" onClick={onRemove}>
              <PlIcon name="trash" />
            </button>
          </div>
        )}
      </div>

      {open && (
        <div className="pl-input-body" id={`${ids}-body`}>
          <Field label="Kind of answer">
            <Segmented
              label="Kind of answer"
              value={param.type || "text"}
              options={TYPES}
              disabled={!canEdit}
              onChange={(type) =>
                onChange({
                  type,
                  default: type === "boolean" ? "false" : param.type === "boolean" ? "" : param.default,
                })
              }
            />
          </Field>

          <div className="pl-grid">
            <Field
              label="Variable name"
              htmlFor={`${ids}-name`}
              error={problemFor("name")}
              hint="What stages read it as. Letters, digits and underscores."
            >
              <input
                id={`${ids}-name`}
                className="is-mono"
                value={param.name || ""}
                placeholder="DEPLOY_ENV"
                disabled={!canEdit}
                spellCheck={false}
                aria-invalid={Boolean(problemFor("name")) || undefined}
                onChange={(event) => onChange({ name: event.target.value })}
              />
            </Field>
            <Field label="Question shown" htmlFor={`${ids}-label`} optional hint="Empty shows the variable name.">
              <input
                id={`${ids}-label`}
                value={param.label || ""}
                placeholder="Environment to deploy to"
                disabled={!canEdit}
                onChange={(event) => onChange({ label: event.target.value })}
              />
            </Field>
            <Field label="Help text" htmlFor={`${ids}-desc`} optional wide>
              <input
                id={`${ids}-desc`}
                value={param.description || ""}
                placeholder="Shown under the field in the Run Build dialog"
                disabled={!canEdit}
                onChange={(event) => onChange({ description: event.target.value })}
              />
            </Field>

            {param.type === "choice" && (
              <Field
                label="Options"
                htmlFor={`${ids}-choices`}
                wide
                error={problemFor("choices")}
                hint="One per line, in the order they are offered."
              >
                <textarea
                  id={`${ids}-choices`}
                  className="is-mono"
                  rows={Math.min(Math.max(choices.length + 1, 3), 8)}
                  spellCheck={false}
                  value={choices.join("\n")}
                  placeholder={"uat\npreprod\nprod"}
                  disabled={!canEdit}
                  onChange={(event) => onChange({ choices: event.target.value.split("\n") })}
                />
              </Field>
            )}

            {param.type === "dynamic_choice" && (
              <Field label="List" hint="Read from the repository each time Run Build opens.">
                <Segmented
                  label="List"
                  value={param.source || "branches"}
                  options={SOURCES}
                  disabled={!canEdit}
                  onChange={(source) => onChange({ source })}
                />
              </Field>
            )}

            {param.type === "boolean" ? (
              <Field label="Starts as">
                <Segmented
                  label="Starts as"
                  value={String(param.default) === "true" ? "true" : "false"}
                  options={[
                    { value: "true", label: "Yes" },
                    { value: "false", label: "No" },
                  ]}
                  disabled={!canEdit}
                  onChange={(value) => onChange({ default: value })}
                />
              </Field>
            ) : param.type === "multiline" ? (
              <Field
                label="Default text"
                htmlFor={`${ids}-default`}
                wide
                optional
                hint={
                  <>
                    Kept whole, newlines and indentation intact — a Dockerfile, an nginx block, a
                    .env. Write it to a file in a stage with{" "}
                    <code>printf '%s' "${param.name || "NAME"}" &gt; file</code>.
                  </>
                }
              >
                <textarea
                  id={`${ids}-default`}
                  className="is-mono"
                  rows={8}
                  spellCheck={false}
                  value={param.default || ""}
                  placeholder={"FROM registry.example.com/nginx\nCOPY ./dist/ /usr/share/nginx/html/"}
                  disabled={!canEdit}
                  onChange={(event) => onChange({ default: event.target.value })}
                />
              </Field>
            ) : param.type === "choice" && choices.filter(Boolean).length ? (
              <Field label="Default" htmlFor={`${ids}-default`} error={problemFor("default")}>
                <select
                  id={`${ids}-default`}
                  value={param.default || ""}
                  disabled={!canEdit}
                  onChange={(event) => onChange({ default: event.target.value })}
                >
                  <option value="">First option</option>
                  {choices.filter(Boolean).map((choice) => (
                    <option key={choice} value={choice}>
                      {choice}
                    </option>
                  ))}
                </select>
              </Field>
            ) : (
              <Field label="Default" htmlFor={`${ids}-default`} optional>
                <input
                  id={`${ids}-default`}
                  className="is-mono"
                  value={param.default || ""}
                  disabled={!canEdit}
                  onChange={(event) => onChange({ default: event.target.value })}
                />
              </Field>
            )}

            {param.type !== "boolean" && (
              <Field label="Must be answered">
                <Switch
                  checked={Boolean(param.required)}
                  disabled={!canEdit}
                  label={param.required ? "Required" : "Can be left empty"}
                  onChange={(required) => onChange({ required })}
                />
              </Field>
            )}
          </div>

          {usedBy.length > 0 && (
            <div className="pl-usedby">
              <PlIcon name="branch" />
              <span>Run conditions on</span>
              {usedBy.map(({ stage, index: stageIndex }) => (
                <button
                  key={stageIndex}
                  type="button"
                  className="btn-ghost pl-link"
                  onClick={() => onOpenStage?.(stageIndex)}
                >
                  {stage.name || `stage ${stageIndex + 1}`}
                </button>
              ))}
              <span className="muted">— they follow if you rename it.</span>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * The Run Build dialog's input section, rendered from the draft. Uses the
 * dialog's own classes so the preview and the real thing cannot drift apart
 * in look; the controls are inert.
 */
function RunPreview({ parameters }) {
  return (
    <aside className="pl-preview" aria-label="Run Build dialog preview">
      <div className="pl-preview-head">
        <PlIcon name="eye" />
        <span>
          <strong>Preview</strong>
          <small>What someone running a build sees</small>
        </span>
      </div>
      <div className="pl-preview-dialog" inert>
        <div className="pl-preview-title">
          <PlIcon name="source" /> Run build
        </div>
        <div className="sg-ci-run-field pl-preview-ref">
          Branch or tag
          <select value="default" onChange={() => {}}>
            <option value="default">main</option>
          </select>
        </div>
        {parameters.length > 0 && (
          <div className="sg-ci-run-params">
            {parameters.map((param, index) => {
              const label = `${param.label || param.name || "New input"}${
                param.required && param.type !== "boolean" ? " *" : ""
              }`;
              if (param.type === "boolean") {
                return (
                  <label key={index} className="checkbox-row">
                    <input type="checkbox" checked={String(param.default) === "true"} readOnly />
                    {label}
                    {param.description && <span className="field-hint">{param.description}</span>}
                  </label>
                );
              }
              const choices =
                param.type === "choice"
                  ? (param.choices || []).filter(Boolean)
                  : param.type === "dynamic_choice"
                    ? [param.source === "tags" ? "v1.4.0" : "main", param.source === "tags" ? "v1.3.2" : "develop"]
                    : [];
              return (
                <div key={index} className="sg-ci-run-field">
                  {label}
                  {choices.length ? (
                    <select value={param.default || choices[0]} onChange={() => {}}>
                      {choices.map((choice) => (
                        <option key={choice} value={choice}>
                          {choice}
                        </option>
                      ))}
                    </select>
                  ) : param.type === "multiline" ? (
                    <textarea rows={3} readOnly value={param.default || ""} className="is-mono" />
                  ) : (
                    <input readOnly value={param.default || ""} />
                  )}
                  {param.description && <span className="field-hint">{param.description}</span>}
                </div>
              );
            })}
          </div>
        )}
        <div className="pl-preview-actions">
          <span className="pl-preview-btn">Cancel</span>
          <span className="pl-preview-btn is-primary">Start build</span>
        </div>
      </div>
    </aside>
  );
}
