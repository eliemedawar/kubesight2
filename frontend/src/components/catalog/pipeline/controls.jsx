import { useEffect, useId, useRef, useState } from "react";
import { PlIcon } from "./icons.jsx";

/**
 * The pipeline editor's controls. Each one edits a value in the shape the API
 * stores it, so nothing upstream has to translate — the translation between
 * "what a person types" and "what is saved" lives here, once.
 */

/** An on/off switch that is a real checkbox underneath. */
export function Switch({ checked, onChange, disabled, label, describedBy }) {
  return (
    <label className={`pl-switch${checked ? " is-on" : ""}${disabled ? " is-disabled" : ""}`}>
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="pl-switch-track" aria-hidden="true">
        <span className="pl-switch-thumb" />
      </span>
      {label && <span className="pl-switch-label">{label}</span>}
    </label>
  );
}

/** One-of-N as a row of buttons. Radio semantics, so arrows move the choice. */
export function Segmented({ value, options, onChange, disabled, label, size }) {
  const refs = useRef([]);
  const current = Math.max(
    0,
    options.findIndex((option) => option.value === value)
  );
  const choose = (index) => {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    refs.current[index]?.focus();
  };
  return (
    <div
      className={`pl-seg${size === "lg" ? " pl-seg--lg" : ""}`}
      role="radiogroup"
      aria-label={label}
      onKeyDown={(event) => {
        if (disabled) return;
        const step =
          event.key === "ArrowRight" || event.key === "ArrowDown"
            ? 1
            : event.key === "ArrowLeft" || event.key === "ArrowUp"
              ? -1
              : 0;
        if (!step) return;
        event.preventDefault();
        choose((current + step + options.length) % options.length);
      }}
    >
      {options.map((option, index) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            ref={(node) => {
              refs.current[index] = node;
            }}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected || (current === -1 && index === 0) ? 0 : -1}
            disabled={disabled || option.disabled}
            className={`btn-ghost pl-seg-option${selected ? " is-on" : ""}`}
            onClick={() => choose(index)}
          >
            {option.icon && <PlIcon name={option.icon} />}
            <span className="pl-seg-copy">
              <strong>{option.label}</strong>
              {option.hint && <small>{option.hint}</small>}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/**
 * A setting that says its current value while closed.
 *
 * The old editor had eight accordions that all read "Optional" until opened,
 * so nothing said which ones mattered. Here the closed row IS the summary: a
 * set value is written in full and marked, an unset one is quiet.
 */
export function SettingRow({ icon, title, hint, value, isSet, tone, defaultOpen, children, id }) {
  const [open, setOpen] = useState(Boolean(defaultOpen));
  const bodyId = useId();
  useEffect(() => {
    if (defaultOpen) setOpen(true);
  }, [defaultOpen]);
  return (
    <section
      className={`pl-setting${open ? " is-open" : ""}${isSet ? " is-set" : ""}${
        tone ? ` is-${tone}` : ""
      }`}
      id={id}
    >
      <button
        type="button"
        className="btn-ghost pl-setting-head"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="pl-setting-icon" aria-hidden="true">
          <PlIcon name={icon} />
        </span>
        <span className="pl-setting-copy">
          <strong>{title}</strong>
          {hint && <small>{hint}</small>}
        </span>
        <span className="pl-setting-value">{value}</span>
        <PlIcon name="chevron" className="pl-setting-chevron" />
      </button>
      {open && (
        <div className="pl-setting-body" id={bodyId}>
          {children}
        </div>
      )}
    </section>
  );
}

/**
 * A textarea whose stored form cannot represent everything a person types.
 *
 * Commands are stored as an array, so re-serialising on every keystroke would
 * delete the blank line you just made with Enter — the value snaps back and
 * the key appears dead. Hold the raw text while the field has focus, publish
 * as you type so nothing is lost on save, and re-sync on blur.
 */
function useDraft(value) {
  const [draft, setDraft] = useState(value);
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setDraft(value);
  }, [value]);
  return {
    draft,
    setDraft,
    bind: {
      onFocus: () => {
        focused.current = true;
      },
      onBlur: () => {
        focused.current = false;
        setDraft(value);
      },
    },
  };
}

/** Shell commands with a line gutter — a script, so it reads like one. */
export function CommandEditor({ lines, onChange, disabled, placeholder, invalid, id }) {
  const value = (lines || []).join("\n");
  const { draft, setDraft, bind } = useDraft(value);
  const gutter = useRef(null);
  const count = Math.max(1, String(draft || "").split("\n").length);
  return (
    <div className={`pl-code${disabled ? " is-disabled" : ""}${invalid ? " is-invalid" : ""}`}>
      <div className="pl-code-bar" aria-hidden="true">
        <span className="pl-code-dots">
          <i />
          <i />
          <i />
        </span>
        <span>sh · runs top to bottom, stops at the first failing line</span>
      </div>
      <div className="pl-code-body">
        <pre className="pl-code-gutter" ref={gutter} aria-hidden="true">
          {Array.from({ length: count }, (_, index) => index + 1).join("\n")}
        </pre>
        <textarea
          id={id}
          value={draft}
          rows={Math.min(Math.max(count + 1, 7), 22)}
          wrap="off"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          disabled={disabled}
          placeholder={placeholder}
          aria-invalid={invalid || undefined}
          {...bind}
          onScroll={(event) => {
            if (gutter.current) gutter.current.scrollTop = event.currentTarget.scrollTop;
          }}
          onChange={(event) => {
            setDraft(event.target.value);
            // Lines are kept verbatim: they join back into one shell script,
            // where a heredoc's blank lines and indentation are content.
            onChange(event.target.value.split("\n"));
          }}
        />
      </div>
    </div>
  );
}

/** A plain multi-line field with the same draft handling. */
export function DraftTextarea({ value, onChangeText, ...props }) {
  const { draft, setDraft, bind } = useDraft(value);
  return (
    <textarea
      {...props}
      value={draft}
      {...bind}
      onChange={(event) => {
        setDraft(event.target.value);
        onChangeText(event.target.value);
      }}
    />
  );
}

/**
 * Rows of small fields for a list-shaped value (variables, host aliases,
 * files). Holds its own rows so a half-filled one survives — the stored form
 * drops a row with no key — and re-reads the value whenever it changes from
 * outside (discard, undo, a different stage).
 */
function useRows(value, toRows, fromRows) {
  const [rows, setRows] = useState(() => toRows(value));
  const published = useRef(JSON.stringify(fromRows(rows)));
  useEffect(() => {
    const incoming = JSON.stringify(value);
    if (incoming !== published.current) {
      setRows(toRows(value));
      published.current = incoming;
    }
    // toRows is a module-level function per editor; value is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const update = (next, onChange) => {
    setRows(next);
    const stored = fromRows(next);
    published.current = JSON.stringify(stored);
    onChange(stored);
  };
  return [rows, update];
}

function RowsTable({ columns, rows, setRows, disabled, addLabel, emptyText, onPasteLines }) {
  const blank = () => Object.fromEntries(columns.map((column) => [column.key, ""]));
  return (
    <div className="pl-rows">
      {rows.length === 0 && <p className="pl-rows-empty">{emptyText}</p>}
      {rows.length > 0 && (
        <div
          className="pl-rows-grid"
          style={{
            gridTemplateColumns: `${columns.map((column) => column.width || "1fr").join(" ")} 2rem`,
          }}
          role="table"
        >
          <div className="pl-rows-head" role="row">
            {columns.map((column) => (
              <span key={column.key} role="columnheader">
                {column.label}
              </span>
            ))}
            <span aria-hidden="true" />
          </div>
          {rows.map((row, index) => (
            <div className="pl-rows-row" role="row" key={index}>
              {columns.map((column) => (
                <input
                  key={column.key}
                  role="cell"
                  className={column.mono ? "is-mono" : ""}
                  value={row[column.key] ?? ""}
                  placeholder={column.placeholder}
                  aria-label={`${column.label}, row ${index + 1}`}
                  list={column.list}
                  disabled={disabled}
                  spellCheck={false}
                  onPaste={
                    onPasteLines && column.key === columns[0].key
                      ? (event) => {
                          const text = event.clipboardData.getData("text");
                          const parsed = onPasteLines(text);
                          if (!parsed) return;
                          event.preventDefault();
                          const next = [...rows];
                          next.splice(index, 1, ...parsed);
                          setRows(next);
                        }
                      : undefined
                  }
                  onChange={(event) =>
                    setRows(
                      rows.map((item, position) =>
                        position === index ? { ...item, [column.key]: event.target.value } : item
                      )
                    )
                  }
                />
              ))}
              <button
                type="button"
                className="btn-ghost pl-rows-remove"
                aria-label={`Remove row ${index + 1}`}
                title="Remove"
                disabled={disabled}
                onClick={() => setRows(rows.filter((_, position) => position !== index))}
              >
                <PlIcon name="x" />
              </button>
            </div>
          ))}
        </div>
      )}
      {!disabled && (
        <button type="button" className="btn-ghost pl-rows-add" onClick={() => setRows([...rows, blank()])}>
          <PlIcon name="plus" /> {addLabel}
        </button>
      )}
    </div>
  );
}

// "KEY=value" lines, the way people paste them out of a .env or a Jenkinsfile.
const parseEnvLines = (text) => {
  if (!/\n|=/.test(text)) return null;
  const rows = String(text)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const clean = line.replace(/^export\s+/, "");
      const index = clean.indexOf("=");
      return index > 0
        ? { key: clean.slice(0, index).trim(), value: clean.slice(index + 1) }
        : { key: clean, value: "" };
    });
  return rows.length ? rows : null;
};

const envToRows = (env) =>
  Object.entries(env || {}).map(([key, value]) => ({ key, value: String(value ?? "") }));
const rowsToEnv = (rows) => {
  const out = {};
  for (const row of rows) {
    const key = String(row.key || "").trim();
    if (key) out[key] = row.value ?? "";
  }
  return out;
};

export function EnvRows({ value, onChange, disabled, keyPlaceholder, valuePlaceholder }) {
  const [rows, update] = useRows(value, envToRows, rowsToEnv);
  return (
    <RowsTable
      columns={[
        { key: "key", label: "Name", placeholder: keyPlaceholder || "MAVEN_OPTS", mono: true, width: "minmax(0, 0.8fr)" },
        { key: "value", label: "Value", placeholder: valuePlaceholder || "-Xmx2g", mono: true, width: "minmax(0, 1.2fr)" },
      ]}
      rows={rows}
      setRows={(next) => update(next, onChange)}
      disabled={disabled}
      addLabel="Add variable"
      emptyText="No variables. Paste KEY=value lines into a name field to add several at once."
      onPasteLines={parseEnvLines}
    />
  );
}

const aliasesToRows = (aliases) =>
  (aliases || []).map((entry) => ({ ip: entry.ip || "", hosts: (entry.hostnames || []).join(", ") }));
// Parsed leniently here — the backend is the validator, so a half-typed row
// does not throw while somebody is still typing it.
const rowsToAliases = (rows) =>
  rows
    .map((row) => ({
      ip: String(row.ip || "").trim(),
      hostnames: String(row.hosts || "")
        .split(/[,\s]+/)
        .map((name) => name.trim())
        .filter(Boolean),
    }))
    .filter((entry) => entry.ip);
const parseAliasLines = (text) => {
  if (!/\n|=|\s/.test(text.trim())) return null;
  const rows = String(text)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const [ip, ...rest] = line.includes("=") ? line.split("=") : line.split(/\s+/);
      return { ip: ip.trim(), hosts: rest.join(" ").replace(/\s+/g, ", ") };
    });
  return rows.length ? rows : null;
};

export function AliasRows({ value, onChange, disabled }) {
  const [rows, update] = useRows(value, aliasesToRows, rowsToAliases);
  return (
    <RowsTable
      columns={[
        { key: "ip", label: "IP address", placeholder: "10.10.10.20", mono: true, width: "minmax(0, 0.7fr)" },
        { key: "hosts", label: "Hostnames", placeholder: "nexus.areeba.com, nexus", mono: true, width: "minmax(0, 1.3fr)" },
      ]}
      rows={rows}
      setRows={(next) => update(next, onChange)}
      disabled={disabled}
      addLabel="Add host alias"
      emptyText="No host aliases. Names resolve through the cluster's DNS. Paste /etc/hosts lines to add several."
      onPasteLines={parseAliasLines}
    />
  );
}

export const ARTIFACT_TYPES = [
  "jar",
  "war",
  "zip",
  "tar",
  "apk",
  "aab",
  "ipa",
  "test-report",
  "coverage",
  "binary",
];

const artifactsToRows = (artifacts) =>
  (artifacts || []).map((item) => ({ path: item.path || "", type: item.type || "" }));
const rowsToArtifacts = (rows) =>
  rows
    .map((row) => ({
      path: String(row.path || "").trim(),
      type: String(row.type || "").trim() || "binary",
    }))
    .filter((item) => item.path);

export function ArtifactRows({ value, onChange, disabled }) {
  const listId = useId();
  const [rows, update] = useRows(value, artifactsToRows, rowsToArtifacts);
  return (
    <>
      <datalist id={listId}>
        {ARTIFACT_TYPES.map((type) => (
          <option key={type} value={type} />
        ))}
      </datalist>
      <RowsTable
        columns={[
          { key: "path", label: "Path or pattern", placeholder: "target/*.jar", mono: true, width: "minmax(0, 1.4fr)" },
          { key: "type", label: "Kind", placeholder: "binary", list: listId, width: "minmax(0, 0.6fr)" },
        ]}
        rows={rows}
        setRows={(next) => update(next, onChange)}
        disabled={disabled}
        addLabel="Add file"
        emptyText="Nothing is kept. Files left in the workspace are gone when the build ends."
      />
    </>
  );
}

/** Tags typed as words: Enter or comma makes a chip, Backspace takes one back.
 * Lowercased by default (runner capabilities are); branch names are not. */
export function ChipsInput({ value, onChange, disabled, placeholder, label, lowercase = true }) {
  const [text, setText] = useState("");
  const items = value || [];
  const commit = (raw) => {
    const next = String(raw)
      .split(/[,\s]+/)
      .map((item) => (lowercase ? item.trim().toLowerCase() : item.trim()))
      .filter(Boolean)
      .filter((item) => !items.includes(item));
    if (next.length) onChange([...items, ...next]);
    setText("");
  };
  return (
    <div className={`pl-chips${disabled ? " is-disabled" : ""}`}>
      {items.map((item) => (
        <span className="pl-chip" key={item}>
          {item}
          {!disabled && (
            <button
              type="button"
              className="btn-ghost"
              aria-label={`Remove ${item}`}
              onClick={() => onChange(items.filter((other) => other !== item))}
            >
              <PlIcon name="x" />
            </button>
          )}
        </span>
      ))}
      {!disabled && (
        <input
          value={text}
          aria-label={label}
          placeholder={items.length ? "" : placeholder}
          onChange={(event) => {
            const next = event.target.value;
            if (/[,\s]$/.test(next)) commit(next);
            else setText(next);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit(text);
            } else if (event.key === "Backspace" && !text && items.length) {
              onChange(items.slice(0, -1));
            }
          }}
          onBlur={() => text && commit(text)}
        />
      )}
    </div>
  );
}

/** A labelled field block: label, control, and a hint or an error under it. */
export function Field({ label, htmlFor, hint, error, children, wide, optional }) {
  return (
    <div className={`pl-field${wide ? " is-wide" : ""}${error ? " has-error" : ""}`}>
      <label htmlFor={htmlFor} className="pl-field-label">
        {label}
        {optional && <span className="pl-field-optional">Optional</span>}
      </label>
      {children}
      {error ? (
        <p className="pl-field-error" role="alert">
          <PlIcon name="alert" /> {error}
        </p>
      ) : (
        hint && <p className="pl-field-hint">{hint}</p>
      )}
    </div>
  );
}
