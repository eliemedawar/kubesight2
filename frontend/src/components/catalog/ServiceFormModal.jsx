import { useEffect, useState } from "react";
import { listCiPipelineTemplates } from "../../api/ciApi.js";
import { APPLICATION_TYPES, CRITICALITIES, applicationTypeLabel } from "./ciShared.jsx";

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
    applicationType: service?.applicationType || "java_maven",
  });

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  // What the chosen type will actually produce. Shown rather than described,
  // because the build image carries the versions and a label claiming "JDK 11"
  // would go stale the moment the image is repointed.
  const [kits, setKits] = useState({});
  useEffect(() => {
    if (isEdit) return undefined;
    let live = true;
    listCiPipelineTemplates()
      .then((data) => {
        if (!live) return;
        const byType = {};
        (data.items || []).forEach((item) => {
          byType[item.applicationType] = item;
        });
        setKits(byType);
      })
      // The preview is a courtesy; registering works without it.
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [isEdit]);
  const kit = kits[form.applicationType];

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
            >
              {/* Legacy types stay selectable only for a service that already
                  carries one, so editing it does not silently retype it. */}
              {APPLICATION_TYPES.filter(
                (type) => !type.legacy || type.value === service?.applicationType
              ).map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
            {!isEdit && !kit && (
              <span className="field-hint">
                Sets the starter pipeline, its build parameters, and the
                Dockerfile — all editable afterwards.
              </span>
            )}
            {isEdit && (
              <span className="field-hint">
                Changing this changes the starter kit and the fallback pipeline.
                A pipeline you have already saved is never rewritten by it.
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
          {!isEdit && kit && (
            <div className="form-grid__full sg-ci-kit">
              <p className="muted">
                <strong>{applicationTypeLabel(form.applicationType)}</strong> starts with{" "}
                {kit.stageNames.join(" → ")}
                {kit.dockerfile ? ", and a Dockerfile" : ""}. All editable afterwards.
              </p>
              <ul>
                {kit.buildImages.length > 0 && (
                  <li>
                    Builds on <code>{kit.buildImages.join(", ")}</code>
                  </li>
                )}
                {kit.parameters.length > 0 && (
                  <li>
                    Asks before each build:{" "}
                    {kit.parameters.map((param) => param.label).join(", ")}
                  </li>
                )}
                {kit.expectedSecrets.length > 0 && (
                  <li>
                    Usually needs the secrets{" "}
                    {kit.expectedSecrets.map((item) => item.key).join(", ")}
                  </li>
                )}
              </ul>
            </div>
          )}

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
