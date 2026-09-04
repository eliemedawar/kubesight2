import { useEffect, useState } from "react";
import { updateCiService } from "../../api/ciApi.js";

const PLACEHOLDER = `FROM registry.example.com/openjdk:11-jre-slim
WORKDIR /app
ADD app.jar app.jar
ENTRYPOINT ["java","-jar","/app/app.jar"]`;

/**
 * Dockerfile tab: the image recipe for repositories that do not carry one.
 *
 * Saved on the service and handed to BuildKit as a file mounted beside the
 * build context — the checkout is never modified, so what the image stage
 * builds from is exactly what was cloned. Leaving this empty keeps the
 * original behaviour: the Dockerfile committed in the repository is used.
 */
export default function DockerfilePanel({ service, canEdit, onSaved }) {
  const [text, setText] = useState(service.dockerfile || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  // Re-seed when the service is reloaded (or a different one is opened) so the
  // editor never shows a previous service's recipe.
  useEffect(() => {
    setText(service.dockerfile || "");
    setSaved(false);
  }, [service.id, service.dockerfile]);

  const dirty = (service.dockerfile || "") !== text;

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      await updateCiService(service.id, { dockerfile: text });
      setSaved(true);
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
          {text.trim()
            ? "Container image stages build with this file. It is mounted beside the build context, so the repository is never modified."
            : "Empty — container image stages use the Dockerfile committed in the repository (or the path set as DOCKERFILE_PATH on the stage)."}
        </p>

        <label className="form-grid__full">
          <textarea
            className="sg-ci-dockerfile"
            rows={18}
            spellCheck={false}
            value={text}
            placeholder={PLACEHOLDER}
            disabled={!canEdit}
            onChange={(event) => {
              setText(event.target.value);
              setSaved(false);
            }}
          />
          <span className="field-hint">
            The build context is the checkout, so <code>ADD app.jar</code> refers to a
            file an earlier stage produced there.
          </span>
        </label>

        {canEdit && (
          <div className="modal-actions">
            {saved && !dirty && <span className="muted">Saved.</span>}
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
