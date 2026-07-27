import { useCallback, useEffect, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoAddSectionModal from "./ZohoAddSectionModal.jsx";
import ZohoBindingModal from "./ZohoBindingModal.jsx";
import ZohoConvertFieldModal from "./ZohoConvertFieldModal.jsx";
import ZohoLayoutSection from "./ZohoLayoutSection.jsx";
import ZohoSourcePicker from "./ZohoSourcePicker.jsx";
import { IconAlert } from "./icons.jsx";
import { CREATABLE_TYPES, linesToValues, valuesToLines } from "./zohoFieldMeta";
import {
  createZohoField,
  getZohoLayout,
  setZohoFieldOptions,
  updateZohoField,
} from "../../api/zohoApi.js";

export default function ZohoLayoutEditor({
  canManage = false,
  config = {},
  reloadKey = 0,
  onSourceSaved,
  onLayoutChanged,
}) {
  const selectedNamespaces = config.selectedNamespaces || [];
  const customEnvironments = config.customEnvironments || [];
  const [layout, setLayout] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // modal state: { mode: 'options'|'editField'|'addField'|'source'|'addSection', field?, initial? }
  const [modal, setModal] = useState(null);
  const [saving, setSaving] = useState(false);
  const [modalError, setModalError] = useState("");

  const sectionNames = (layout?.sections || []).map((s) => s.name).filter(Boolean);

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
    onLayoutChanged?.();
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
      if (form.sectionName) payload.sectionName = form.sectionName;
      if (form.type === "Picklist") payload.values = linesToValues(form.values);
      const created = await createZohoField(payload);
      // Creating the field and placing it are two Zoho calls; if the placement
      // half failed the field still exists, so say where it actually landed.
      const warning = (created?.warnings || [])[0];
      await afterSave(
        warning
          ? `Field "${form.label}" created — ${warning}`
          : `Field "${form.label}" created${
              created?.sectionName ? ` in ${created.sectionName}` : ""
            }.`
      );
    } catch (err) {
      setModalError(err.message || "Failed to create the field.");
      setSaving(false);
    }
  };

  const onFieldAction = (action, field) => {
    if (action === "source") {
      setModal({ mode: "source" });
    } else if (action === "options") {
      setModal({
        mode: "options",
        field,
        initial: {
          values: valuesToLines(field.allowedValues),
          defaultValue: field.defaultValue || "-None-",
          required: field.required,
        },
      });
    } else if (action === "bind") {
      setModal({ mode: "binding", field });
    } else if (action === "edit") {
      setModal({
        mode: "editField",
        field,
        initial: { label: field.label, required: field.required },
      });
    } else if (action === "convert") {
      setModal({ mode: "convert", field });
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
        <ErrorBanner message={error} />
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
          <div className="sg-zh-head-tools">
            <button
              type="button"
              className="btn-ghost"
              onClick={() => setModal({ mode: "addSection" })}
            >
              Add section
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => setModal({ mode: "addField" })}
            >
              Add field
            </button>
          </div>
        ) : null}
      </div>

      {/* Source + cascade summary */}
      <div className="sg-zh-src">
        <span className="sg-zh-src-label">Source</span>
        {config.sourceClusterId || customEnvironments.length ? (
          <>
            {config.sourceClusterId ? (
              <span className="sg-tag">{config.sourceClusterId}</span>
            ) : null}
            {selectedNamespaces.map((ns) => (
              <span key={ns} className="sg-tag">{ns}</span>
            ))}
            {customEnvironments.map((c) => (
              <span key={c.name} className="sg-tag sg-tag-custom" title="Custom environment (Jenkins-only)">
                {c.name}
              </span>
            ))}
            {!selectedNamespaces.length && !customEnvironments.length ? (
              <span>no namespaces selected</span>
            ) : null}
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
        <ZohoLayoutSection
          key={section.name}
          section={section}
          config={config}
          canManage={canManage}
          onAddField={(sectionName) => setModal({ mode: "addField", sectionName })}
          onAction={onFieldAction}
        />
      ))}

      {(layout?.sections || []).length === 0 ? (
        <EmptyState message="No sections found on this layout." />
      ) : null}

      {modal?.mode === "source" ? (
        <ZohoSourcePicker
          initialClusterId={config.sourceClusterId || ""}
          initialNamespaces={selectedNamespaces}
          initialDeployments={config.selectedDeployments || {}}
          initialCustom={customEnvironments}
          initialOverrides={config.jobOverrides || []}
          onClose={closeModal}
          onSaved={(data) => {
            closeModal();
            setNotice("Source saved. Run “Sync now” to publish it to Zoho.");
            onSourceSaved?.(data);
          }}
        />
      ) : modal?.mode === "binding" ? (
        <ZohoBindingModal
          field={modal.field}
          onClose={closeModal}
          onSaved={async (message) => {
            await afterSave(message);
          }}
        />
      ) : modal?.mode === "convert" ? (
        <ZohoConvertFieldModal
          field={modal.field}
          onClose={closeModal}
          onSaved={async (message) => {
            await afterSave(message);
          }}
        />
      ) : modal?.mode === "addSection" ? (
        <ZohoAddSectionModal
          onClose={closeModal}
          onSaved={async (name) => {
            await afterSave(`Section "${name}" added.`);
          }}
        />
      ) : modal ? (
        <FieldModal
          modal={modal}
          sectionNames={sectionNames}
          creatableTypes={layout?.creatableTypes || CREATABLE_TYPES}
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

function FieldModal({
  modal,
  sectionNames,
  creatableTypes,
  saving,
  error,
  onClose,
  onSaveOptions,
  onSaveEdit,
  onSaveNew,
}) {
  const [form, setForm] = useState(() => {
    if (modal.mode === "addField") {
      return {
        label: "",
        type: "Text",
        required: false,
        values: "",
        // Pre-selected when the operator used a section's own "Add field here".
        sectionName: modal.sectionName || sectionNames[0] || "",
      };
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
  // The default must be one of the options — a typed value that doesn't match
  // is silently coerced to -None- by the backend, which reads as a bug.
  const defaultChoices = ["-None-", ...linesToValues(form.values || "")];

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel"
        role="dialog"
        aria-modal="true"
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
        {error ? <ErrorBanner message={error} /> : null}

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
                  {creatableTypes.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Section
                <select
                  value={form.sectionName}
                  onChange={(e) => set("sectionName", e.target.value)}
                >
                  {sectionNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <span className="field-hint">
                  Placing the field rewrites the whole layout — KubeSight verifies every existing
                  field survives before saving.
                </span>
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
                  <select
                    value={
                      defaultChoices.includes(form.defaultValue) ? form.defaultValue : "-None-"
                    }
                    onChange={(e) => set("defaultValue", e.target.value)}
                  >
                    {defaultChoices.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
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
