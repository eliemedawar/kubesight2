import { useCallback, useEffect, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoSourcePicker from "./ZohoSourcePicker.jsx";
import {
  createZohoField,
  getZohoLayout,
  setZohoFieldOptions,
  updateZohoField,
} from "../../api/zohoApi.js";

const CREATABLE_TYPES = ["Text", "Textarea", "Picklist", "Number", "Decimal", "Date", "DateTime", "Boolean", "Email", "Phone", "URL"];

// Values are stored one-per-line in the editor; -None- is managed by the backend.
const linesToValues = (text) =>
  text.split("\n").map((l) => l.trim()).filter((l) => l && l !== "-None-");
const valuesToLines = (values) =>
  (values || []).filter((v) => v !== "-None-").join("\n");

export default function ZohoLayoutEditor({ canManage = false, config = {}, onSourceSaved }) {
  const envFieldId = String(config.environmentFieldId || "");
  const appFieldId = String(config.appFieldId || "");
  const selectedNamespaces = config.selectedNamespaces || [];
  const [layout, setLayout] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // modal state: { mode: 'options'|'editField'|'addField', field?, section? }
  const [modal, setModal] = useState(null);
  const [saving, setSaving] = useState(false);
  const [modalError, setModalError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setLayout(await getZohoLayout());
    } catch (err) {
      setError(err.message || "Failed to read the Zoho layout.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const closeModal = () => {
    setModal(null);
    setModalError("");
    setSaving(false);
  };

  const afterSave = async (msg) => {
    setNotice(msg);
    closeModal();
    await load();
  };

  const saveOptions = async (form) => {
    setSaving(true);
    setModalError("");
    try {
      await setZohoFieldOptions(modal.field.id, {
        values: linesToValues(form.values),
        defaultValue: form.defaultValue || "-None-",
        isMandatory: form.required,
      });
      await afterSave(`Options updated for "${modal.field.label}".`);
    } catch (err) {
      setModalError(err.message || "Failed to update options.");
      setSaving(false);
    }
  };

  const saveFieldEdit = async (form) => {
    setSaving(true);
    setModalError("");
    try {
      await updateZohoField(modal.field.id, { label: form.label, required: form.required });
      await afterSave(`Field "${form.label}" updated.`);
    } catch (err) {
      setModalError(err.message || "Failed to update the field.");
      setSaving(false);
    }
  };

  const saveNewField = async (form) => {
    setSaving(true);
    setModalError("");
    try {
      const payload = { label: form.label, type: form.type, required: form.required };
      if (form.type === "Picklist") payload.values = linesToValues(form.values);
      await createZohoField(payload);
      await afterSave(`Field "${form.label}" created.`);
    } catch (err) {
      setModalError(err.message || "Failed to create the field.");
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <section className="card">
        <h3>DevOps Request layout</h3>
        <p className="muted">Reading the layout from Zoho…</p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="card">
        <h3>DevOps Request layout</h3>
        <ErrorBanner message={error} onDismiss={() => setError("")} />
        <button type="button" className="secondary" onClick={load}>
          Retry
        </button>
      </section>
    );
  }

  return (
    <section className="card zoho-layout">
      <div className="card-header-row">
        <div>
          <h3>{layout?.layoutName || "DevOps Request"} — layout fields</h3>
          <p className="muted">
            A live mirror of the Zoho Desk layout. Manage dropdown options and fields here; every
            change is written straight to Zoho (this layout only).
          </p>
        </div>
        {canManage ? (
          <button type="button" className="primary" onClick={() => setModal({ mode: "addField" })}>
            Add field
          </button>
        ) : null}
      </div>

      {/* Source + cascade summary */}
      <p className="field-hint" style={{ marginTop: 0 }}>
        Dropdown source:{" "}
        {config.sourceClusterId ? (
          <>
            cluster <span className="mono">{config.sourceClusterId}</span> ·{" "}
            {selectedNamespaces.length
              ? `${selectedNamespaces.length} namespace(s): `
              : "no namespaces selected "}
            {selectedNamespaces.map((ns) => (
              <span key={ns} className="badge status-muted mono" style={{ marginRight: 4 }}>
                {ns}
              </span>
            ))}
          </>
        ) : (
          "not set — use “Choose namespaces” on the Environment field below."
        )}
        {" · "}
        Cascade Env→App:{" "}
        <span
          className={`badge ${
            config.lastDependencyStatus === "ok"
              ? "status-ok"
              : config.lastDependencyStatus === "error"
              ? "status-error"
              : "status-muted"
          }`}
        >
          {config.cascadeEnabled === false
            ? "off"
            : config.lastDependencyStatus || "pending"}
        </span>
        {config.lastDependencyStatus === "error" && config.lastDependencyMessage ? (
          <span className="muted"> — {config.lastDependencyMessage}</span>
        ) : null}
      </p>

      {notice ? (
        <div className="banner banner-success" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      {(layout?.sections || []).map((section) => (
        <div key={section.name} className="zoho-section">
          <h4 className="zoho-section__title">{section.name}</h4>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
              gap: "12px",
            }}
          >
            {section.fields.map((field) => (
              <div
                key={field.id || field.apiName}
                className="card"
                style={{ padding: "12px 14px", margin: 0 }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                  <div>
                    <strong>
                      {field.label}
                      {field.required ? <span className="status-error"> *</span> : null}
                    </strong>
                    <div className="muted mono" style={{ fontSize: "0.8em" }}>
                      {field.apiName}
                    </div>
                  </div>
                  <div style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <span className="badge status-muted">{field.type}</span>
                    {field.autoManaged ? (
                      <div className="badge status-ok" style={{ marginTop: 4 }} title="Published by the KubeSight sync (deployments / namespaces). Manual edits here are overwritten on the next sync.">
                        auto-synced
                      </div>
                    ) : null}
                  </div>
                </div>

                {field.isPicklist ? (
                  <div style={{ marginTop: 8 }}>
                    <div className="chip-row">
                      {(field.allowedValues || [])
                        .filter((v) => v !== "-None-")
                        .slice(0, 8)
                        .map((v) => (
                          <span key={v} className="badge status-muted mono">
                            {v}
                          </span>
                        ))}
                      {(field.allowedValues || []).filter((v) => v !== "-None-").length === 0 ? (
                        <span className="muted">no options</span>
                      ) : null}
                      {(field.allowedValues || []).filter((v) => v !== "-None-").length > 8 ? (
                        <span className="muted">
                          +{(field.allowedValues || []).filter((v) => v !== "-None-").length - 8} more
                        </span>
                      ) : null}
                    </div>
                    {String(field.id) === appFieldId ? (
                      <div className="field-hint" style={{ marginTop: 4 }}>
                        Auto-derived live from the selected namespaces' deployments — manage it via
                        “Choose namespaces” on the Environment field.
                      </div>
                    ) : null}
                  </div>
                ) : null}

                {canManage ? (
                  <div className="actions" style={{ marginTop: 10 }}>
                    {field.isPicklist && String(field.id) === envFieldId ? (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => setModal({ mode: "source" })}
                      >
                        Choose namespaces
                      </button>
                    ) : field.isPicklist && String(field.id) === appFieldId ? null : field.isPicklist ? (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() =>
                          setModal({
                            mode: "options",
                            field,
                            initial: {
                              values: valuesToLines(field.allowedValues),
                              defaultValue: field.defaultValue || "-None-",
                              required: field.required,
                            },
                          })
                        }
                      >
                        Manage options
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="link-button"
                      onClick={() =>
                        setModal({
                          mode: "editField",
                          field,
                          initial: { label: field.label, required: field.required },
                        })
                      }
                    >
                      Edit
                    </button>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ))}

      {modal?.mode === "source" ? (
        <ZohoSourcePicker
          initialClusterId={config.sourceClusterId || ""}
          initialNamespaces={selectedNamespaces}
          initialDeployments={config.selectedDeployments || {}}
          onClose={closeModal}
          onSaved={(data) => {
            closeModal();
            setNotice("Source saved. Run “Sync now” to publish it to Zoho.");
            onSourceSaved?.(data);
          }}
        />
      ) : modal ? (
        <FieldModal
          modal={modal}
          saving={saving}
          error={modalError}
          onClose={closeModal}
          onSaveOptions={saveOptions}
          onSaveEdit={saveFieldEdit}
          onSaveNew={saveNewField}
        />
      ) : null}
    </section>
  );
}

function FieldModal({ modal, saving, error, onClose, onSaveOptions, onSaveEdit, onSaveNew }) {
  const [form, setForm] = useState(() => {
    if (modal.mode === "addField") {
      return { label: "", type: "Text", required: false, values: "" };
    }
    return modal.initial || {};
  });
  const set = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  const title =
    modal.mode === "options"
      ? `Manage options — ${modal.field.label}`
      : modal.mode === "editField"
      ? `Edit field — ${modal.field.label}`
      : "Add a field";

  const submit = (e) => {
    e.preventDefault();
    if (modal.mode === "options") onSaveOptions(form);
    else if (modal.mode === "editField") onSaveEdit(form);
    else onSaveNew(form);
  };

  const isPicklistCreate = modal.mode === "addField" && form.type === "Picklist";

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel"
        role="dialog"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h3>{title}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {modal.field?.autoManaged ? (
          <p className="field-hint">
            ⚠️ This field is auto-published by the KubeSight sync — manual changes here will be
            overwritten on the next sync.
          </p>
        ) : null}
        {error ? <ErrorBanner message={error} onDismiss={() => {}} /> : null}

        <form className="settings-form" onSubmit={submit}>
          {modal.mode === "addField" ? (
            <>
              <label>
                Label
                <input value={form.label} onChange={(e) => set("label", e.target.value)} required />
              </label>
              <label>
                Type
                <select value={form.type} onChange={(e) => set("type", e.target.value)}>
                  {CREATABLE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
            </>
          ) : null}

          {modal.mode === "editField" ? (
            <label>
              Label
              <input value={form.label} onChange={(e) => set("label", e.target.value)} required />
            </label>
          ) : null}

          {(modal.mode === "options" || isPicklistCreate) ? (
            <>
              <label>
                Dropdown options (one per line)
                <textarea
                  rows={8}
                  value={form.values || ""}
                  onChange={(e) => set("values", e.target.value)}
                  placeholder={"option-a\noption-b"}
                  className="mono"
                />
                <span className="field-hint">
                  <code>-None-</code> is kept automatically as the first option. No special
                  characters / emojis (Zoho rejects them).
                </span>
              </label>
              {modal.mode === "options" ? (
                <label>
                  Default value
                  <input
                    value={form.defaultValue || "-None-"}
                    onChange={(e) => set("defaultValue", e.target.value)}
                  />
                </label>
              ) : null}
            </>
          ) : null}

          {modal.mode !== "addField" || true ? (
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={Boolean(form.required)}
                onChange={(e) => set("required", e.target.checked)}
              />
              Required field
            </label>
          ) : null}

          <div className="modal-actions">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="primary" disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
