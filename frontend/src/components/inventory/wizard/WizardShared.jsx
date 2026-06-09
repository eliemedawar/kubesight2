export function WizardSectionHeader({ title, subtitle }) {
  return (
    <div className="wizard-section-header">
      <div>
        <h4 className="wizard-section-header__title">{title}</h4>
        {subtitle ? <p className="muted">{subtitle}</p> : null}
      </div>
    </div>
  );
}

export function Field({ label, children, hint, tooltip }) {
  return (
    <label className="wizard-field">
      <span className="wizard-field__label">
        {label}
        {tooltip ? (
          <span className="wizard-field__tooltip" title={tooltip} aria-label={tooltip}>
            ⓘ
          </span>
        ) : null}
      </span>
      {children}
      {hint ? <span className="wizard-field__hint muted">{hint}</span> : null}
    </label>
  );
}

export function KeyValueEditor({
  items = [],
  onChange,
  keyPlaceholder = "key",
  valuePlaceholder = "value",
  emptyRow = { key: "", value: "" },
}) {
  const rows = items.length ? items : [{ ...emptyRow }];
  const keyField = emptyRow.name !== undefined ? "name" : "key";

  const update = (index, field, val) => {
    const next = rows.map((r, i) => (i === index ? { ...r, [field]: val } : r));
    onChange(next);
  };

  const add = () => onChange([...rows, { ...emptyRow }]);

  const remove = (index) => {
    if (rows.length <= 1) {
      onChange([{ ...emptyRow }]);
      return;
    }
    onChange(rows.filter((_, i) => i !== index));
  };

  return (
    <div className="wizard-repeatable-list">
      {rows.map((row, index) => (
        <div key={index} className="wizard-kv-row wizard-repeatable-row">
          <input
            placeholder={keyPlaceholder}
            value={row[keyField] ?? ""}
            onChange={(e) => update(index, keyField, e.target.value)}
          />
          <input
            placeholder={valuePlaceholder}
            value={row.value ?? ""}
            onChange={(e) => update(index, "value", e.target.value)}
          />
          <button type="button" className="btn-outline btn-sm" onClick={() => remove(index)} aria-label="Remove row">
            Remove
          </button>
        </div>
      ))}
      <button type="button" className="btn-outline btn-sm wizard-repeatable-add" onClick={add}>
        + Add
      </button>
    </div>
  );
}

export function PortListEditor({ ports = [], onChange }) {
  const rows = ports.length ? ports : [""];

  const commitPorts = (rawRows) => {
    const parsed = rawRows
      .map((p) => parseInt(String(p).trim(), 10))
      .filter((p) => Number.isFinite(p) && p > 0);
    onChange(parsed.length ? parsed : [8080]);
  };

  const update = (index, value) => {
    const next = rows.map((p, i) => (i === index ? value : p));
    commitPorts(next);
  };

  const add = () => onChange([...(ports.length ? ports : [8080]), 8080]);

  const remove = (index) => {
    if (rows.length <= 1) {
      onChange([8080]);
      return;
    }
    onChange(ports.filter((_, i) => i !== index));
  };

  return (
    <div className="wizard-repeatable-list">
      {rows.map((port, index) => (
        <div key={index} className="wizard-kv-row wizard-repeatable-row">
          <input
            type="number"
            min={1}
            max={65535}
            placeholder="80"
            value={port}
            onChange={(e) => update(index, e.target.value)}
          />
          <button type="button" className="btn-outline btn-sm" onClick={() => remove(index)} aria-label="Remove port">
            Remove
          </button>
        </div>
      ))}
      <button type="button" className="btn-outline btn-sm wizard-repeatable-add" onClick={add}>
        + Add port
      </button>
    </div>
  );
}

export function MountedFilesEditor({ items = [], onChange }) {
  const rows = items.length ? items : [{ name: "", mountPath: "", configMap: "", subPath: "" }];

  const update = (index, field, value) => {
    const next = rows.map((row, i) => (i === index ? { ...row, [field]: value } : row));
    onChange(next);
  };

  const add = () => onChange([...rows, { name: "", mountPath: "", configMap: "", subPath: "" }]);

  const remove = (index) => {
    if (rows.length <= 1) {
      onChange([{ name: "", mountPath: "", configMap: "", subPath: "" }]);
      return;
    }
    onChange(rows.filter((_, i) => i !== index));
  };

  return (
    <div className="wizard-repeatable-list">
      {rows.map((row, index) => (
        <div key={index} className="wizard-mount-row wizard-repeatable-row">
          <input placeholder="Volume name" value={row.name} onChange={(e) => update(index, "name", e.target.value)} />
          <input placeholder="Mount path" value={row.mountPath} onChange={(e) => update(index, "mountPath", e.target.value)} />
          <input placeholder="ConfigMap name" value={row.configMap} onChange={(e) => update(index, "configMap", e.target.value)} />
          <input placeholder="Sub path (optional)" value={row.subPath || ""} onChange={(e) => update(index, "subPath", e.target.value)} />
          <button type="button" className="btn-outline btn-sm" onClick={() => remove(index)} aria-label="Remove mount">
            Remove
          </button>
        </div>
      ))}
      <button type="button" className="btn-outline btn-sm wizard-repeatable-add" onClick={add}>
        + Add mount
      </button>
    </div>
  );
}

export function VolumeMountsEditor({ items = [], onChange }) {
  const rows = items.length ? items : [{ name: "", mountPath: "", readOnly: false }];

  const update = (index, field, value) => {
    const next = rows.map((row, i) => (i === index ? { ...row, [field]: value } : row));
    onChange(next);
  };

  const add = () => onChange([...rows, { name: "", mountPath: "", readOnly: false }]);

  const remove = (index) => {
    if (rows.length <= 1) {
      onChange([{ name: "", mountPath: "", readOnly: false }]);
      return;
    }
    onChange(rows.filter((_, i) => i !== index));
  };

  return (
    <div className="wizard-repeatable-list">
      {rows.map((row, index) => (
        <div key={index} className="wizard-mount-row wizard-repeatable-row">
          <input placeholder="Volume name" value={row.name} onChange={(e) => update(index, "name", e.target.value)} />
          <input placeholder="Mount path" value={row.mountPath} onChange={(e) => update(index, "mountPath", e.target.value)} />
          <label className="wizard-checkbox wizard-checkbox--inline">
            <input type="checkbox" checked={Boolean(row.readOnly)} onChange={(e) => update(index, "readOnly", e.target.checked)} />
            Read-only
          </label>
          <button type="button" className="btn-outline btn-sm" onClick={() => remove(index)} aria-label="Remove volume mount">
            Remove
          </button>
        </div>
      ))}
      <button type="button" className="btn-outline btn-sm wizard-repeatable-add" onClick={add}>
        + Add volume mount
      </button>
    </div>
  );
}
