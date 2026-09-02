import { useState } from "react";
import { APPLICATION_TYPES, CRITICALITIES } from "./ciShared.jsx";

/**
 * Register or rename a service.
 *
 * Deliberately identity-only: the repository lives on the Source tab and the
 * stages on the Pipeline tab. Asking for all three up front would make
 * registering a service a research task.
 */
export default function ServiceFormModal({ service, onClose, onSave, saving, error }) {
  const isEdit = Boolean(service?.id);
  const [form, setForm] = useState({
    name: service?.name || "",
    slug: service?.slug || "",
    description: service?.description || "",
    ownerTeam: service?.ownerTeam || "",
    criticality: service?.criticality || "medium",
    applicationType: service?.applicationType || "java",
  });

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  // Mirrors the backend's _slug() so the placeholder shows what an empty
  // field will produce.
  const derivedSlug =
    form.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "service";

  const submit = () => {
    if (!form.name.trim()) return;
    // Empty slug means "derive from the name" on create and "keep" on edit.
    onSave({ ...form, slug: form.slug.trim() });
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card"
        role="dialog"
        aria-label={isEdit ? "Edit service" : "Register service"}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>{isEdit ? "Edit service" : "Register a service"}</h3>
          <p className="muted">
            {isEdit
              ? "Identity and ownership. Source and pipeline are edited on their own tabs."
              : "Name the application. You will connect its repository and pipeline next."}
          </p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <div className="form-grid">
          <label className="form-grid__full">
            Name *
            <input
              value={form.name}
              maxLength={160}
              placeholder="e.g. Payment Service"
              onChange={(event) => set("name", event.target.value)}
            />
          </label>
          <label className="form-grid__full">
            Slug (build identifier)
            <input
              value={form.slug}
              maxLength={180}
              placeholder={derivedSlug}
              onChange={(event) =>
                set("slug", event.target.value.toLowerCase().replace(/[^a-z0-9-]+/g, "-"))
              }
            />
            <span className="field-hint">
              Names the pushed image (<code>{form.slug.trim() || derivedSlug}:&lt;tag&gt;</code>).
              For ticket-driven deploys it must equal the Kubernetes deployment name —
              that is how automation finds this service.
            </span>
          </label>
          <label className="form-grid__full">
            Description
            <textarea
              rows={2}
              style={{ resize: "vertical" }}
              value={form.description}
              onChange={(event) => set("description", event.target.value)}
            />
          </label>
          <label>
            Application type
            <select
              value={form.applicationType}
              onChange={(event) => set("applicationType", event.target.value)}
              disabled={isEdit}
            >
              {APPLICATION_TYPES.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
            {!isEdit && (
              <span className="field-hint">Sets the starter pipeline.</span>
            )}
            {isEdit && (
              <span className="field-hint">
                Fixed after registration — it decided the starter pipeline.
              </span>
            )}
          </label>
          <label>
            Criticality
            <select
              value={form.criticality}
              onChange={(event) => set("criticality", event.target.value)}
            >
              {CRITICALITIES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="form-grid__full">
            Owner / team
            <input
              value={form.ownerTeam}
              maxLength={255}
              placeholder="e.g. Payments"
              onChange={(event) => set("ownerTeam", event.target.value)}
            />
          </label>
        </div>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            onClick={submit}
            disabled={saving || !form.name.trim()}
          >
            {saving ? "Saving…" : isEdit ? "Save changes" : "Register service"}
          </button>
        </div>
      </div>
    </div>
  );
}
