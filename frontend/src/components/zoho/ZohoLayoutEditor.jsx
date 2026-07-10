import { useCallback, useEffect, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoSourcePicker from "./ZohoSourcePicker.jsx";
import { IconAlert } from "./icons.jsx";
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

const VALUE_CHIP_CAP = 8;

export default function ZohoLayoutEditor({
  canManage = false,
  config = {},
  reloadKey = 0,
  onSourceSaved,
}) {
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

  const load = useCallback(async (fresh = false) => {
    setLoading(true);
    setError("");
    try {
      setLayout(await getZohoLayout(fresh));
    } catch (err) {
      setError(err.message || "Failed to read the Zoho layout.");
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial mount reads via the server cache; a page-level Refresh (reloadKey
  // bump) re-reads straight from Zoho.
  useEffect(() => {
    load(reloadKey > 0);
  }, [load, reloadKey]);

  const closeModal = () => {
    setModal(null);
    setModalError("");
    setSaving(false);
  };

  const afterSave = async (msg) => {
    setNotice(msg);
    closeModal();
    await load(true);
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
        <button type="button" className="secondary" onClick={() => load(true)}>
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
          <button type="button" className="secondary" onClick={() => setModal({ mode: "addField" })}>
            Add field
          </button>
        ) : null}
      </div>

      {/* Source + cascade summary */}
      <div className="sg-zh-src">
        <span className="sg-zh-src-label">Source</span>
        {config.sourceClusterId ? (
          <>
            <span className="sg-tag">{config.sourceClusterId}</span>
            {selectedNamespaces.length ? (
              selectedNamespaces.map((ns) => (
                <span key={ns} className="sg-tag">{ns}</span>
              ))
            ) : (
              <span>no namespaces selected</span>
            )}
          </>
        ) : (
          <span>not set — use “Choose namespaces” on the Environment field below.</span>
        )}
        <span className="sg-zh-src-sep">·</span>
        <span className="sg-zh-src-label">Cascade Env→App</span>
        <span
          className={`status-pill ${
            config.lastDependencyStatus === "ok"
              ? "ok"
              : config.lastDependencyStatus === "error"
              ? "danger"
              : "muted"
          }`}
        >
          {config.cascadeEnabled === false ? "Off" : config.lastDependencyStatus || "Pending"}
        </span>
        {config.lastDependencyStatus === "error" && config.lastDependencyMessage ? (
          <span className="muted">— {config.lastDependencyMessage}</span>
        ) : null}
      </div>

      {notice ? (
        <div className="banner banner-success" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      {(layout?.sections || []).map((section) => (
        <div key={section.name}>
          <h4 className="sg-zh-sect">{section.name}</h4>
          <div className="sg-zh-fgrid">
            {section.fields.map((field) => {
              const values = (field.allowedValues || []).filter((v) => v !== "-None-");
              return (
                <div key={field.id || field.apiName} className="sg-zh-field">
                  <header>
                    <div className="sg-zh-fname">
                      <b>
                        {field.label}
                        {field.required ? (
                          <span className="sg-zh-req" title="Required">*</span>
                        ) : null}
                      </b>
                      <span className="sg-zh-fapi">{field.apiName}</span>
                    </div>
                    <div className="sg-zh-fbadges">
                      <span className="sg-tag">{field.type}</span>
                      {field.autoManaged ? (
                        <span
                          className="status-pill ok"
                          title="Published by the KubeSight sync (deployments / namespaces). Manual edits here are overwritten on the next sync."
                        >
                          auto-synced
                        </span>
                      ) : null}
                    </div>
                  </header>

                  {field.isPicklist ? (
                    <>
                      <div className="sg-zh-tags sg-zh-fvals">
                        {values.slice(0, VALUE_CHIP_CAP).map((v) => (
                          <span key={v} className="sg-tag">{v}</span>
                        ))}
                        {values.length === 0 ? <span className="muted">no options</span> : null}
                        {values.length > VALUE_CHIP_CAP ? (
                          <span className="sg-zh-more">+{values.length - VALUE_CHIP_CAP} more</span>
                        ) : null}
                      </div>
                      {String(field.id) === appFieldId ? (
                        <div className="sg-zh-fhint">
                          Auto-derived live from the selected namespaces' deployments — manage it via
                          “Choose namespaces” on the Environment field.
                        </div>
                      ) : null}
                    </>
                  ) : null}

                  {canManage ? (
                    <footer>
                      {field.isPicklist && String(field.id) === envFieldId ? (
                        <button
                          type="button"
                          className="btn-outline"
                          onClick={() => setModal({ mode: "source" })}
                        >
                          Choose namespaces
                        </button>
                      ) : field.isPicklist && String(field.id) === appFieldId ? null : field.isPicklist ? (
                        <button
                          type="button"
                          className="btn-ghost"
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
                        className="btn-ghost"
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
                    </footer>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ))}

      {(layout?.sections || []).length === 0 ? (
        <EmptyState message="No sections found on this layout." />
      ) : null}

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
          <p className="sg-zh-note">
            <IconAlert />
            <span>
              {modal.mode === "options"
                ? "This field's options are auto-published by the KubeSight sync — manual changes to the option list will be overwritten on the next sync."
                : "This field's options are auto-published by the KubeSight sync. Label and required changes made here are kept."}
            </span>
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
              <label className="field-span">
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

          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={Boolean(form.required)}
              onChange={(e) => set("required", e.target.checked)}
            />
            Required field
          </label>

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
