import { useRef, useState } from "react";
import { CopyButton } from "../zoho/common.jsx";
import { IconAlert, IconCheck } from "../zoho/icons.jsx";
import JenkinsSection from "./JenkinsSection.jsx";
import { useTicketing } from "./TicketingContext.jsx";
import { JENKINS_SECTION, railFor, sectionDone, sectionsFor } from "./settingsSchema.js";

// Setup room: sticky completeness rail + grouped sections + a contextual save
// bar. Replaces both the 18-field config modal and the Jenkins connection modal.
//
// The sections themselves come from `settingsSchema` rather than being written
// out here, because Zoho and Jira need genuinely different inputs (an OAuth
// self-client grant vs a static API token; Desk status labels vs Jira workflow
// transitions) but the identical scaffolding around them.

function SettingsCard({ id, title, pill, children, refFn }) {
  return (
    <section className="card sg-zh-setsec" id={id} ref={refFn}>
      <div className="card-header-row">
        <h3>{title}</h3>
        {pill || null}
      </div>
      {children}
    </section>
  );
}

function sectionPill(kind, config) {
  switch (kind) {
    case undefined:
      return null;
    case "enabled":
      return (
        <span className={`status-pill ${config?.enabled ? "ok" : "muted"}`}>
          {config?.enabled ? "Integration on" : "Integration off"}
        </span>
      );
    case "writeback":
      return (
        <span className={`status-pill ${config?.ticketWritebackEnabled ? "ok" : "muted"}`}>
          {config?.ticketWritebackEnabled ? "On" : "Off"}
        </span>
      );
    case "secret":
      return config?.inboundSecretConfigured ? (
        <span className="status-pill ok">Secret configured</span>
      ) : (
        <span className="status-pill warn">Open — no secret set</span>
      );
    default:
      // A literal string is a plain informational chip.
      return <span className="sg-zh-count">{kind}</span>;
  }
}

function Field({ field, form, config, setField, readOnly }) {
  const value = form[field.key];
  const disabled = readOnly;

  if (field.type === "checkbox") {
    return (
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => setField(field.key, e.target.checked)}
          disabled={disabled}
        />
        {field.label}
      </label>
    );
  }

  if (field.type === "select") {
    return (
      <label title={field.title}>
        {field.label}
        <select
          value={value ?? ""}
          onChange={(e) => setField(field.key, e.target.value)}
          disabled={disabled}
        >
          {(field.options || []).map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        {field.hint ? <span className="field-hint">{field.hint}</span> : null}
      </label>
    );
  }

  // A stored secret is never returned by the API, so the placeholder is what
  // tells the operator one already exists and that blank means "keep it".
  const placeholder = field.secretOf
    ? config?.[field.secretOf]
      ? "•••• (leave blank to keep)"
      : ""
    : field.placeholder;

  return (
    <label title={field.title}>
      {field.label}
      <input
        type={field.type === "password" ? "password" : field.type === "number" ? "number" : "text"}
        min={field.min}
        value={value ?? ""}
        onChange={(e) => setField(field.key, e.target.value)}
        placeholder={placeholder}
        autoComplete={field.type === "password" ? "new-password" : "off"}
        disabled={disabled}
      />
      {field.hint ? <span className="field-hint">{field.hint}</span> : null}
    </label>
  );
}

/** Fields in declaration order, with `row`-grouped ones laid out side by side. */
function FieldList({ fields, ...rest }) {
  const out = [];
  let index = 0;
  while (index < fields.length) {
    const field = fields[index];
    if (field.hidden) {
      index += 1;
      continue;
    }
    if (!field.row) {
      out.push(<Field key={field.key} field={field} {...rest} />);
      index += 1;
      continue;
    }
    const group = [];
    while (index < fields.length && fields[index].row === field.row) {
      group.push(fields[index]);
      index += 1;
    }
    out.push(
      <div key={`row-${field.row}`} className="sg-zh-jrow4">
        {group.map((f) => (
          <Field key={f.key} field={f} {...rest} />
        ))}
      </div>
    );
  }
  return out;
}

export default function TicketingSettingsTab({
  canManage,
  config,
  form,
  setField,
  onSaveConfig,
  savingConfig,
  dirty,
  onDiscard,
  webhookUrl,
  jenkins,
  onSaveJenkins,
  savingJenkins,
  onTestJenkins,
  testingJenkins,
}) {
  const { key: providerKey, name: providerName } = useTicketing();
  const sectionRefs = useRef({});
  const rail = railFor(providerKey);
  const [active, setActive] = useState(rail[0]?.key || "connection");
  const ro = !canManage;

  const jumpTo = (key) => {
    setActive(key);
    sectionRefs.current[key]?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="sg-zh-setgrid">
      <nav className="sg-zh-setrail" aria-label="Settings sections">
        {rail.map((item) => {
          const done = sectionDone(providerKey, item.key, config, jenkins);
          return (
            <button
              key={item.key}
              type="button"
              className={`sg-zh-setitem ${active === item.key ? "sg-zh-setitem--on" : ""}`}
              onClick={() => jumpTo(item.key)}
            >
              {item.label}
              <span
                className={`sg-zh-setstate ${done ? "sg-zh-setstate--done" : "sg-zh-setstate--todo"}`}
                title={done ? "Configured" : "Needs attention"}
              >
                {done ? <IconCheck /> : <IconAlert />}
              </span>
            </button>
          );
        })}
        {ro ? (
          <p className="sg-zh-setro">
            Read-only — you don't have the ticketing:manage permission.
          </p>
        ) : null}
      </nav>

      <div className="sg-zh-setmain">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSaveConfig();
          }}
        >
          {sectionsFor(providerKey).map((section) => (
            <SettingsCard
              key={section.key}
              id={`zh-set-${section.key}`}
              title={section.label}
              refFn={(el) => (sectionRefs.current[section.key] = el)}
              pill={sectionPill(section.pill, config)}
            >
              {section.intro ? <p className="muted">{section.intro}</p> : null}

              {section.render === "webhook" ? (
                <div className="sg-zh-url">
                  <input
                    readOnly
                    value={webhookUrl}
                    onFocus={(e) => e.target.select()}
                    className="mono"
                  />
                  <CopyButton text={webhookUrl} />
                </div>
              ) : null}

              <div
                className={`settings-form sg-zh-setform${
                  section.wide ? " sg-zh-fieldmap" : ""
                }`}
              >
                <FieldList
                  fields={section.fields || []}
                  form={form}
                  config={config}
                  setField={setField}
                  readOnly={ro}
                />
              </div>
            </SettingsCard>
          ))}

          {dirty && canManage ? (
            <div className="sg-zh-savebar">
              <span>Unsaved {providerName} configuration changes</span>
              <div className="sg-zh-savebar-actions">
                <button type="button" className="secondary" onClick={onDiscard}>
                  Discard
                </button>
                <button type="submit" className="primary" disabled={savingConfig}>
                  {savingConfig ? "Saving…" : "Save configuration"}
                </button>
              </div>
            </div>
          ) : null}
        </form>

        <JenkinsSection
          canManage={canManage}
          jenkins={jenkins}
          onSave={onSaveJenkins}
          saving={savingJenkins}
          onTest={onTestJenkins}
          testing={testingJenkins}
          refFn={(el) => (sectionRefs.current[JENKINS_SECTION.key] = el)}
        />
      </div>
    </div>
  );
}
