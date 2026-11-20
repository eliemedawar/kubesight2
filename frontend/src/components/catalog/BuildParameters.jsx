import { useEffect, useId, useState } from "react";
import { Field, Segmented, Switch } from "./pipeline/controls.jsx";
import { PlIcon } from "./pipeline/icons.jsx";
import {
  MAX_PARAMETERS,
  moveItem,
  parameterProblems,
  stagesUsingParameter,
} from "./pipeline/stageModel.js";
import { confirmDialog } from "../../lib/dialogs.js";

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

const sharedDefaultOf = (param) =>
  param.type === "boolean" ? (String(param.default) === "true" ? "true" : "false") : String(param.default ?? "");

/**
 * A linked service's own default for one shared input, or null when it has
 * none or the one it has no longer fits (the option was removed on the shared
 * pipeline). Mirrors pipelines.apply_parameter_defaults on the server, which
 * ignores such an entry and builds with the shared default.
 */
export function ownDefaultFor(param, own) {
  if (!own || !param?.name || !Object.prototype.hasOwnProperty.call(own, param.name)) return null;
  const raw = own[param.name];
  if (raw === null || raw === undefined) return null;
  if (param.type === "boolean") return String(raw) === "true" ? "true" : "false";
  const value = param.type === "multiline" ? String(raw) : String(raw).trim();
  if (param.type === "choice" && !(param.choices || []).map(String).includes(value)) return null;
  return value;
}

/** The shared inputs as this service's builds are asked them. */
export function withServiceDefaults(parameters, own) {
  return (parameters || []).map((param) => {
    const value = ownDefaultFor(param, own);
    return value === null ? param : { ...param, default: value };
  });
}

/**
 * What saving sends: only entries that apply, and none equal to the shared
 * default (so "same as shared" keeps following the shared pipeline). An entry
 * the shared pipeline no longer accepts is dropped rather than refused, since
 * the person saving may be changing a different input.
 */
export function fittingServiceDefaults(parameters, own) {
  const out = {};
  for (const param of parameters || []) {
    const value = ownDefaultFor(param, own);
    if (value !== null && value !== sharedDefaultOf(param)) out[param.name] = value;
  }
  return out;
}

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
export default function BuildParameters({
  parameters,
  stages = [],
  canEdit,
  onChange,
  onOpenStage,
  // Set when this service builds with a shared pipeline: { name, serviceName }.
  // The questions are then read-only (edited on the Pipelines page) and each
  // one gets a default of this service's own.
  shared = null,
  serviceDefaults = null,
  canEditDefaults = false,
  onServiceDefaultsChange,
}) {
  const items = parameters || [];
  const sharedMode = Boolean(shared);
  const ownCount = sharedMode ? Object.keys(fittingServiceDefaults(items, serviceDefaults)).length : 0;
  const setOwnDefault = (param, value) => {
    const next = { ...(serviceDefaults || {}) };
    if (value === null || value === sharedDefaultOf(param)) delete next[param.name];
    else next[param.name] = value;
    onServiceDefaultsChange?.(next);
  };
  const [open, setOpen] = useState(items.length ? 0 : null);

  useEffect(() => {
    if (open !== null && open >= items.length) setOpen(items.length ? items.length - 1 : null);
  }, [items.length, open]);

  const mutate = (index, patch) =>
    onChange(items.map((item, position) => (position === index ? { ...item, ...patch } : item)));

  const remove = async (index) => {
    const used = stagesUsingParameter(items[index].name, stages);
    if (
      used.length &&
      !(await confirmDialog({
        title: "Remove input",
        message: `${used.map(({ stage }) => `“${stage.name}”`).join(", ")} ${
          used.length === 1 ? "runs" : "run"
        } only when this input matches. Remove it anyway? Those stages will keep a condition on a name that no longer exists.`,
        confirmLabel: "Remove",
        tone: "danger",
      }))
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
            {sharedMode ? (
              <p>
                These questions come from the shared pipeline “{shared.name}” and are edited there.
                What each one starts as is {shared.serviceName}'s own: set a default here and only
                this service's builds, schedules and webhooks start with it.
                {ownCount > 0 && (
                  <>
                    {" "}
                    <strong>
                      {ownCount} input{ownCount === 1 ? " starts" : "s start"} differently for {shared.serviceName}.
                    </strong>
                  </>
                )}
              </p>
            ) : (
              <p>
                Each answer becomes an environment variable with the same name in every stage, and
                can switch stages on or off with a run condition.
              </p>
            )}
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
              {sharedMode
                ? `Run Build asks only for the branch or tag. Inputs are added on the shared pipeline “${shared.name}”.`
                : "Run Build asks only for the branch or tag. Add an input to ask for an environment, a version, or a yes/no like “Deploy after build?”."}
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
                canEdit={canEdit && !sharedMode}
                onToggle={() => setOpen((current) => (current === index ? null : index))}
                onChange={(patch) => mutate(index, patch)}
                onMove={(delta) => move(index, delta)}
                onRemove={() => remove(index)}
                onOpenStage={onOpenStage}
                own={
                  sharedMode
                    ? {
                        serviceName: shared.serviceName,
                        value: ownDefaultFor(param, serviceDefaults),
                        stale:
                          Boolean(serviceDefaults) &&
                          Object.prototype.hasOwnProperty.call(serviceDefaults, param.name) &&
                          ownDefaultFor(param, serviceDefaults) === null,
                        canEdit: canEditDefaults,
                        onChange: (value) => setOwnDefault(param, value),
                      }
                    : null
                }
              />
            ))}
          </ol>
        )}

        <p className="pl-field-hint">
          A container image stage also reads <code>IMAGE_NAME</code> and <code>IMAGE_TAG</code> from
          inputs of those names, and they win over the stage's own values.
        </p>
      </div>

      <RunPreview parameters={sharedMode ? withServiceDefaults(items, serviceDefaults) : items} />
    </div>
  );
}

/**
 * A linked service's own default for one shared input. The control matches
 * the input's kind, so a choice can only start at one of its options.
 */
function OwnDefault({ param, own, ids }) {
  const shared = sharedDefaultOf(param);
  const value = own.value ?? shared;
  const overridden = own.value !== null;
  const choices = (param.choices || []).filter(Boolean);
  const id = `${ids}-own`;
  const label = `Default for ${own.serviceName}`;
  const sharedText = param.type === "boolean" ? (shared === "true" ? "Yes" : "No") : shared || "empty";
  const hint = overridden
    ? `The shared pipeline starts it at “${sharedText}”; ${own.serviceName}'s builds start at this instead.`
    : `Same as the shared pipeline. Change it to start ${own.serviceName}'s builds somewhere else.`;

  let control;
  if (param.type === "boolean") {
    control = (
      <Segmented
        label={label}
        value={value === "true" ? "true" : "false"}
        options={[
          { value: "true", label: "Yes" },
          { value: "false", label: "No" },
        ]}
        disabled={!own.canEdit}
        onChange={own.onChange}
      />
    );
  } else if (param.type === "choice" && choices.length) {
    control = (
      <select id={id} value={choices.includes(value) ? value : choices[0]} disabled={!own.canEdit} onChange={(event) => own.onChange(event.target.value)}>
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
            {choice === shared ? " (shared default)" : ""}
          </option>
        ))}
      </select>
    );
  } else if (param.type === "multiline") {
    control = (
      <textarea
        id={id}
        className="is-mono"
        rows={6}
        spellCheck={false}
        value={value}
        disabled={!own.canEdit}
        onChange={(event) => own.onChange(event.target.value)}
      />
    );
  } else {
    control = (
      <input
        id={id}
        className="is-mono"
        value={value}
        placeholder={param.type === "dynamic_choice" ? "A branch or tag name" : ""}
        disabled={!own.canEdit}
        spellCheck={false}
        onChange={(event) => own.onChange(event.target.value)}
      />
    );
  }

  return (
    <div className="pl-own-default">
      <div className="pl-own-default-row">
        <Field label={label} htmlFor={param.type === "boolean" ? undefined : id} hint={hint}>
          {control}
        </Field>
        {overridden && own.canEdit && (
          <button type="button" className="btn-outline btn-compact" onClick={() => own.onChange(null)}>
            <PlIcon name="reset" /> Use shared default
          </button>
        )}
      </div>
      {own.stale && (
        <p className="pl-own-default-stale" role="status">
          <PlIcon name="alert" />
          The default {own.serviceName} had is no longer one of this input's options, so its builds
          start at the shared default. Saving clears it.
        </p>
      )}
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
  own = null,
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
              {own?.value != null && (
                <span className="pl-tag is-accent" title={`${own.serviceName}'s builds start at this`}>
                  Starts at “{param.type === "boolean" ? (own.value === "true" ? "Yes" : "No") : own.value.split("\n")[0].slice(0, 40)}” here
                </span>
              )}
              {own?.stale && (
                <span className="pl-tag is-error">
                  <PlIcon name="alert" /> Own default no longer fits
                </span>
              )}
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
          {own && <OwnDefault param={param} own={own} ids={ids} />}
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
              <Field label={own ? "Shared default" : "Starts as"}>
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
                label={own ? "Shared default text" : "Default text"}
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
              <Field label={own ? "Shared default" : "Default"} htmlFor={`${ids}-default`} error={problemFor("default")}>
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
              <Field label={own ? "Shared default" : "Default"} htmlFor={`${ids}-default`} optional>
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
