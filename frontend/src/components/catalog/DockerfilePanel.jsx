import { useEffect, useState } from "react";
import { listCiPipelineTemplates, updateCiService } from "../../api/ciApi.js";
import { applicationTypeLabel } from "./ciShared.jsx";

/**
 * Dockerfile tab: the image recipe for the service.
 *
 * Saved on the service and handed to BuildKit as a file mounted beside the
 * build context — the checkout is never modified, so what the image stage
 * builds from is exactly what was cloned.
 *
 * The recipe follows the application type and is not something to choose here:
 * registering a service writes its type's template, and a service that somehow
 * has none gets it filled in below. There is no template picker, because a
 * Gradle service wanting the Maven recipe is a wrong application type rather
 * than a Dockerfile to go shopping for.
 */
export default function DockerfilePanel({ service, canEdit, onSaved }) {
  const [text, setText] = useState(service.dockerfile || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  // Filled in from the service's own type when it has no recipe of its own.
  const [prefilled, setPrefilled] = useState(false);

  // Re-seed when the service is reloaded (or a different one is opened) so the
  // editor never shows a previous service's recipe.
  useEffect(() => {
    setText(service.dockerfile || "");
    setPrefilled(false);
  }, [service.id, service.dockerfile]);

  // Saving reloads the service, which re-runs the effect above. Clearing the
  // confirmation there would make it flash and vanish on every successful save,
  // so it is cleared when a different service is opened and on the next edit.
  useEffect(() => {
    setSaved(false);
  }, [service.id]);

  // A service registered before its type had a template — or one whose recipe
  // was cleared — gets the type's recipe put in front of it rather than an
  // empty box it has no way to fill.
  useEffect(() => {
    if (service.dockerfile) return undefined;
    let live = true;
    listCiPipelineTemplates()
      .then((data) => {
        if (!live) return;
        const own = (data.items || []).find(
          (item) => item.applicationType === service.applicationType
        );
        if (own?.dockerfile) {
          setText(own.dockerfile);
          setPrefilled(true);
        }
      })
      // No template means an empty box, which is the honest state for a type
      // that defines no recipe. Not worth a banner.
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [service.id, service.dockerfile, service.applicationType]);

  const dirty = (service.dockerfile || "") !== text;
  const filled = Boolean(text.trim());

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      await updateCiService(service.id, { dockerfile: text });
      setSaved(true);
      setPrefilled(false);
      onSaved?.();
    } catch (err) {
      setError(err.message || "Could not save the Dockerfile.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      <section className="form-section">
        <h4>Dockerfile</h4>
        <p className="muted sg-ci-dockerfile-note">
          {prefilled
            ? `Prefilled from the ${applicationTypeLabel(
                service.applicationType
              )} template. Save it to build with it.`
            : filled
              ? "Container image stages build this file. It is mounted beside the build context, so the repository is never modified."
              : "Empty — container image stages use the Dockerfile committed in the repository (or the path set as DOCKERFILE_PATH on the stage)."}
        </p>

        <label className="form-grid__full">
          <textarea
            className="sg-ci-dockerfile"
            rows={18}
            spellCheck={false}
            value={text}
            disabled={!canEdit}
            onChange={(event) => {
              setText(event.target.value);
              setSaved(false);
              setPrefilled(false);
            }}
          />
          <span className="field-hint">
            The build context is the checkout, so <code>COPY app.jar</code> refers to a
            file an earlier stage produced there.
          </span>
        </label>

        {canEdit && (
          <div className="modal-actions">
            {saved && !dirty && <span className="muted">Saved.</span>}
            {dirty && <span className="muted">Unsaved changes.</span>}
            <button
              type="button"
              className="primary"
              disabled={saving || !dirty}
              onClick={save}
            >
              {saving ? "Saving…" : "Save Dockerfile"}
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
