import { useCallback, useEffect, useMemo, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ConfirmActionModal from "../inventory/ConfirmActionModal.jsx";
import ZohoAddSectionModal from "./ZohoAddSectionModal.jsx";
import ZohoBindingModal from "./ZohoBindingModal.jsx";
import ZohoConvertFieldModal from "./ZohoConvertFieldModal.jsx";
import ZohoLayoutSection from "./ZohoLayoutSection.jsx";
import ZohoMoveFieldModal from "./ZohoMoveFieldModal.jsx";
import ZohoRecoveryModal from "./ZohoRecoveryModal.jsx";
import ZohoRenameSectionModal from "./ZohoRenameSectionModal.jsx";
import ZohoSourcePicker from "./ZohoSourcePicker.jsx";
import { IconAlert, IconHistory, IconRefresh, IconSearch } from "./icons.jsx";
import { CREATABLE_TYPES, linesToValues, valuesToLines } from "./zohoFieldMeta";
import { useTicketing } from "../ticketing/TicketingContext.jsx";

export default function ZohoLayoutEditor({
  canManage = false,
  config = {},
  reloadKey = 0,
  onSourceSaved,
  onLayoutChanged,
}) {
  const { name: providerName, formNoun, can, capabilities, api } = useTicketing();
  const selectedNamespaces = config.selectedNamespaces || [];
  const customEnvironments = config.customEnvironments || [];
  const [layout, setLayout] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [collapsedSections, setCollapsedSections] = useState(() => new Set());

  // modal state: { mode: 'options'|'editField'|'addField'|'source', field?, initial? }
  const [modal, setModal] = useState(null);
  const [saving, setSaving] = useState(false);
  const [modalError, setModalError] = useState("");

  const sectionNames = (layout?.sections || []).map((s) => s.name).filter(Boolean);
  const normalizedQuery = query.trim().toLowerCase();
  const visibleSections = useMemo(() => {
    const sections = layout?.sections || [];
    if (!normalizedQuery) {
      return sections.map((section) => ({
        section,
        fields: section.fields || [],
      }));
    }
    return sections
      .map((section) => {
        const fields = (section.fields || []).filter((field) =>
          [
            field.label,
            field.apiName,
            field.type,
            ...(field.allowedValues || []).map((value) =>
              typeof value === "object" ? value?.value : value
            ),
          ].some((value) => String(value || "").toLowerCase().includes(normalizedQuery))
        );
        return { section, fields };
      })
      .filter(({ fields }) => fields.length > 0);
  }, [layout, normalizedQuery]);
  const totalFields = (layout?.sections || []).reduce(
    (count, section) => count + (section.fields || []).length,
    0
  );
  const matchedFields = visibleSections.reduce((count, entry) => count + entry.fields.length, 0);

  const load = useCallback(async (fresh = false) => {
    setLoading(true);
    setError("");
    try {
      setLayout(await api.getLayout(fresh));
    } catch (err) {
      setError(err.message || `Failed to read the ${providerName} ${formNoun}.`);
    } finally {
      setLoading(false);
    }
  }, [api, providerName, formNoun]);

  // Initial mount reads via the server cache; a page-level Refresh (reloadKey
  // bump) re-reads straight from the provider.
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
      const payload = { values: linesToValues(form.values) };
      if (can("fieldOptionDefaults")) payload.defaultValue = form.defaultValue || "-None-";
      if (can("requiredFields")) payload.isMandatory = form.required;
      await api.setFieldOptions(modal.field.id, payload);
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
      const payload = { label: form.label };
      if (can("requiredFields")) payload.required = form.required;
      await api.updateField(modal.field.id, payload);
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
      const payload = { label: form.label, type: form.type };
      if (can("requiredFields")) payload.required = form.required;
      if (form.sectionName) payload.sectionName = form.sectionName;
      if (["Picklist", "Select", "Cascading select"].includes(form.type)) {
        payload.values = linesToValues(form.values);
      }
      const created = await api.createField(payload);
      // Creating the field and placing it are two provider calls; if the placement
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

  const confirmDeleteField = async () => {
    setSaving(true);
    setModalError("");
    try {
      const label = modal.field.label;
      await api.deleteField(modal.field.id, {
        deleteField: capabilities.deleteMode === "trash",
      });
      await afterSave(
        capabilities.deleteMode === "trash"
          ? `Field "${label}" moved to the ${providerName} trash.`
          : `Field "${label}" permanently deleted from ${providerName}.`
      );
    } catch (err) {
      setModalError(err.message || "Failed to delete the field.");
      setSaving(false);
    }
  };

  const onFieldAction = (action, field, sectionName) => {
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
      // Guarded by the same capability the action chip is rendered behind, so a
      // stale menu cannot open a modal whose endpoint answers 501.
      if (can("convertField")) setModal({ mode: "convert", field });
    } else if (action === "move") {
      setModal({ mode: "moveField", field, sectionName });
    } else if (action === "delete") {
      setModal({ mode: "deleteField", field });
    } else if (action === "remove") {
      setModal({ mode: "removeField", field });
    }
  };

  const confirmRemoveField = async () => {
    setSaving(true);
    setModalError("");
    try {
      const label = modal.field.label;
      await api.deleteField(modal.field.id, { deleteField: false });
      await afterSave(`Field "${label}" removed from this ${formNoun}.`);
    } catch (err) {
      setModalError(err.message || `Failed to remove the field from this ${formNoun}.`);
      setSaving(false);
    }
  };

  const sectionKey = (section) => String(section.id ?? section.name);
  const toggleSection = (section) => {
    const key = sectionKey(section);
    setCollapsedSections((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const collapseAll = () =>
    setCollapsedSections(new Set((layout?.sections || []).map((section) => sectionKey(section))));
  const expandAll = () => setCollapsedSections(new Set());

  if (loading) {
    return (
      <section className="card">
        <h3>DevOps Request {formNoun}</h3>
        <p className="muted">Reading the {formNoun} from {providerName}…</p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="card">
        <h3>DevOps Request {formNoun}</h3>
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
          <h3>
            {layout?.layoutName || "DevOps Request"} — {formNoun} fields
          </h3>
          <p className="muted">
            A live mirror of the {providerName} {formNoun}. Section names and field placement
            change this {formNoun}; deleting a custom field can affect tickets across{" "}
            {providerName}, not just this form, and is always confirmed.
          </p>
        </div>
      </div>

      {!can("createSections") ? (
        <div className="sg-zh-layout-guide">
          <IconAlert />
          <span>
            New sections must be created in {providerName}. After adding one there, refresh this{" "}
            {formNoun} to manage its fields here.
          </span>
          <button type="button" className="secondary" onClick={() => load(true)} disabled={loading}>
            <IconRefresh />
            Refresh {formNoun}
          </button>
        </div>
      ) : null}

      <div className="sg-zh-layout-tools">
        <label className="sg-zh-layout-search">
          <IconSearch />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={`Search ${totalFields} fields…`}
            aria-label={`Search ${formNoun} fields`}
          />
        </label>
        <span className="sg-zh-count">
          {normalizedQuery ? `${matchedFields} of ${totalFields}` : `${totalFields} fields`}
        </span>
        <div className="sg-zh-layout-tool-actions">
          <button type="button" className="btn-ghost" onClick={expandAll}>
            Expand all
          </button>
          <button type="button" className="btn-ghost" onClick={collapseAll}>
            Collapse all
          </button>
          {can("layoutRecovery") ? (
            <button
              type="button"
              className="secondary"
              onClick={() => setModal({ mode: "recovery" })}
            >
              <IconHistory />
              Recovery
            </button>
          ) : null}
          {canManage && can("createSections") ? (
            <button
              type="button"
              className="primary"
              onClick={() => setModal({ mode: "addSection" })}
            >
              + Add section
            </button>
          ) : null}
        </div>
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

      {visibleSections.map(({ section, fields }) => (
        <ZohoLayoutSection
          key={section.id || section.name}
          section={section}
          fields={fields}
          totalFieldCount={(section.fields || []).length}
          sectionNames={sectionNames}
          collapsed={!normalizedQuery && collapsedSections.has(sectionKey(section))}
          searching={Boolean(normalizedQuery)}
          config={config}
          canManage={canManage}
          onToggle={() => toggleSection(section)}
          onAddField={(sectionName) => setModal({ mode: "addField", sectionName })}
          onRename={(section) => setModal({ mode: "renameSection", section })}
          onAction={onFieldAction}
        />
      ))}

      {(layout?.sections || []).length === 0 ? (
        <EmptyState message={`No sections found on this ${formNoun}.`} />
      ) : normalizedQuery && visibleSections.length === 0 ? (
        <EmptyState message={`No fields match "${query.trim()}".`} />
      ) : null}

      {modal?.mode === "removeField" ? (
        <ConfirmActionModal
          open
          title={`Remove “${modal.field.label}” from this ${formNoun}?`}
          message={`The field and its issue values remain in ${providerName}. This only removes it from the configured ${formNoun}, and an administrator can add it back later.`}
          confirmLabel={`Remove from ${formNoun}`}
          busy={saving}
          error={modalError}
          onClose={closeModal}
          onConfirm={confirmRemoveField}
        />
      ) : modal?.mode === "deleteField" ? (
        <ConfirmActionModal
          open
          title={
            capabilities.deleteMode === "trash"
              ? `Move “${modal.field.label}” to trash?`
              : `Delete “${modal.field.label}”?`
          }
          message={
            capabilities.deleteWarning ||
            `This permanently deletes the custom field across the ${providerName} organization, including its stored values. This cannot be undone.`
          }
          confirmLabel={
            capabilities.deleteMode === "trash"
              ? `Move field to ${providerName} trash`
              : "Permanently delete field"
          }
          danger
          busy={saving}
          error={modalError}
          onClose={closeModal}
          onConfirm={confirmDeleteField}
        />
      ) : modal?.mode === "addSection" ? (
        <ZohoAddSectionModal onClose={closeModal} onSaved={afterSave} />
      ) : modal?.mode === "renameSection" ? (
        <ZohoRenameSectionModal
          section={modal.section}
          onClose={closeModal}
          onSaved={async (name) => {
            await afterSave(`Section renamed to "${name}".`);
          }}
        />
      ) : modal?.mode === "moveField" ? (
        <ZohoMoveFieldModal
          field={modal.field}
          currentSectionName={modal.sectionName}
          sectionNames={sectionNames}
          onClose={closeModal}
          onSaved={afterSave}
        />
      ) : modal?.mode === "recovery" ? (
        <ZohoRecoveryModal
          canManage={canManage}
          onClose={closeModal}
          onRestored={afterSave}
        />
      ) : modal?.mode === "source" ? (
        <ZohoSourcePicker
          initialClusterId={config.sourceClusterId || ""}
          initialNamespaces={selectedNamespaces}
          initialDeployments={config.selectedDeployments || {}}
          initialCustom={customEnvironments}
          initialOverrides={config.jobOverrides || []}
          onClose={closeModal}
          onSaved={(data) => {
            closeModal();
            setNotice(`Source saved. Run “Sync now” to publish it to ${providerName}.`);
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
  const { name: providerName, can, api } = useTicketing();
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
  const [loadingDetails, setLoadingDetails] = useState(
    modal.mode === "options" && can("lazyFieldOptions") && modal.field?.allowedValues == null
  );
  const [detailsError, setDetailsError] = useState("");
  const set = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  useEffect(() => {
    if (!loadingDetails) return undefined;
    let active = true;
    api
      .getField(modal.field.id)
      .then((field) => {
        if (!active) return;
        setForm((previous) => ({
          ...previous,
          values: valuesToLines(field.allowedValues),
          defaultValue: field.defaultValue || "-None-",
          required: field.required,
        }));
      })
      .catch((err) => {
        if (active) setDetailsError(err.message || "Could not load this field's options.");
      })
      .finally(() => {
        if (active) setLoadingDetails(false);
      });
    return () => {
      active = false;
    };
  }, [api, loadingDetails, modal.field?.id]);

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

  const isPicklistCreate =
    modal.mode === "addField" && ["Picklist", "Select", "Cascading select"].includes(form.type);
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
                : `This field's options are auto-published by the KubeSight sync. ${
                    can("requiredFields") ? "Label and required changes" : "Label changes"
                  } made here are kept.`}
            </span>
          </p>
        ) : null}
        {error || detailsError ? <ErrorBanner message={error || detailsError} /> : null}

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
                  required
                >
                  {sectionNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <span className="field-hint">
                  {can("layoutPlan")
                    ? "Placing the field rewrites the whole layout — KubeSight verifies every existing field survives before saving."
                    : `The field is created and added directly to this ${providerName} section.`}
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
                  {loadingDetails ? (
                    `Loading the current options from ${providerName}…`
                  ) : can("fieldOptionDefaults") ? (
                    <>
                      <code>-None-</code> is kept automatically as the first option. No special
                      characters / emojis ({providerName} rejects them).
                    </>
                  ) : (
                    `Options removed from this list are disabled in ${providerName} so existing issues keep their history.`
                  )}
                </span>
              </label>
              {modal.mode === "options" && can("fieldOptionDefaults") ? (
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

          {can("requiredFields") ? (
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
            <button
              type="submit"
              className="primary"
              disabled={saving || loadingDetails || Boolean(detailsError)}
            >
              {saving ? "Saving…" : loadingDetails ? "Loading…" : "Save"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
