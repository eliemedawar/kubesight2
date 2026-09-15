import { useEffect, useMemo, useState } from "react";
import {
  listCiPipelineTemplates,
  readCiSourceFile,
  updateCiService,
} from "../../api/ciApi.js";
import { APPLICATION_TYPES } from "./ciShared.jsx";

/**
 * Dockerfile tab: the image recipe for the service.
 *
 * Saved on the service and handed to BuildKit as a file mounted beside the
 * build context — the checkout is never modified, so what the image stage
 * builds from is exactly what was cloned. Leaving this empty keeps the original
 * behaviour: the Dockerfile committed in the repository is used.
 *
 * Registering a Java, Node or Python service seeds this box from the
 * application type's template, which means the inline recipe is now the default
 * rather than the exception. That makes one fact worth stating loudly rather
 * than burying: a filled box OVERRIDES the repository's own Dockerfile. Hence
 * the banner, and the Check repository button that answers the question
 * directly instead of leaving it to be discovered in a build log.
 */
export default function DockerfilePanel({ service, canEdit, onSaved }) {
  const [text, setText] = useState(service.dockerfile || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const [templates, setTemplates] = useState([]);
  const [templateType, setTemplateType] = useState(service.applicationType || "generic");
  // null = not asked yet. Checking the repository costs an API call to
  // Bitbucket, so it happens on demand rather than on every tab open.
  const [repoCheck, setRepoCheck] = useState(null);
  const [checking, setChecking] = useState(false);

  // Re-seed when the service is reloaded (or a different one is opened) so the
  // editor never shows a previous service's recipe.
  useEffect(() => {
    setText(service.dockerfile || "");
    setTemplateType(service.applicationType || "generic");
    setRepoCheck(null);
  }, [service.id, service.dockerfile, service.applicationType]);

  // Saving reloads the service, which re-runs the effect above. Clearing the
  // confirmation there would make it flash and vanish on every successful save,
  // so it is cleared when a different service is opened and on the next edit.
  useEffect(() => {
    setSaved(false);
  }, [service.id]);

  useEffect(() => {
    let live = true;
    listCiPipelineTemplates()
      .then((data) => live && setTemplates(data.items || []))
      // A template list that will not load costs the Insert button, nothing
      // else — the editor stays usable, so this is not worth a banner.
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  const templateByType = useMemo(() => {
    const map = {};
    templates.forEach((item) => {
      map[item.applicationType] = item;
    });
    return map;
  }, [templates]);

  const chosen = templateByType[templateType];
  const chosenBody = chosen?.dockerfile || "";
  const ownType = service.applicationType || "generic";
  // Why a type defines no recipe. Worth saying: "Insert template" sitting
  // permanently disabled reads as broken rather than as deliberate.
  const noTemplateReason =
    {
      container:
        "This type builds the Dockerfile committed in the repository, so it defines none.",
      generic:
        "Nothing is known about what a generic service builds, so it defines no Dockerfile.",
    }[templateType] ||
    "This type produces an APK, AAB or IPA rather than an image, so it defines no Dockerfile.";
  const dirty = (service.dockerfile || "") !== text;
  const filled = Boolean(text.trim());

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

  const checkRepository = async () => {
    setChecking(true);
    setError("");
    try {
      const data = await readCiSourceFile(service.id, "Dockerfile");
      setRepoCheck({ found: true, path: data.path, revision: data.revision });
    } catch (err) {
      // A missing file and an unreachable repository both arrive as a 400 with
      // a readable message. Showing it verbatim is more honest than deciding
      // which of the two it was.
      setRepoCheck({ found: false, message: err.message || "" });
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      <section className="form-section">
        <h4>Dockerfile</h4>
        <p className="muted sg-ci-dockerfile-note">
          {filled
            ? "Container image stages build this file. It is mounted beside the build context, so the repository is never modified."
            : "Empty — container image stages use the Dockerfile committed in the repository (or the path set as DOCKERFILE_PATH on the stage)."}
        </p>

        {filled && (
          <p className="banner-message warning-banner">
            This file takes precedence over the one in the repository. Clear the box
            and save to hand the job back to the repository.{" "}
            {service.sourceConfigured && (
              <button
                type="button"
                className="btn-link"
                disabled={checking}
                onClick={checkRepository}
              >
                {checking ? "Checking…" : "Check the repository"}
              </button>
            )}
          </p>
        )}

        {repoCheck && (
          <p className="muted sg-ci-dockerfile-note">
            {repoCheck.found
              ? `The repository also has ${repoCheck.path} on ${repoCheck.revision}. The file below is what gets built.`
              : `No Dockerfile read from the repository — ${repoCheck.message}`}
          </p>
        )}

        {/* The template list needs ci_pipelines:view, which someone who can
            edit a service may not hold. Rather than show a picker and a
            permanently disabled button with no explanation, the row appears
            only once the list is actually in hand. */}
        {canEdit && templates.length > 0 && (
          <div className="sg-ci-dockerfile-tools">
            <label>
              Template
              <select
                value={templateType}
                onChange={(event) => setTemplateType(event.target.value)}
              >
                {APPLICATION_TYPES.map((type) => (
                  <option key={type.value} value={type.value}>
                    {type.label}
                    {type.value === ownType ? " (this service)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="btn-outline"
              disabled={!chosenBody || chosenBody === text}
              onClick={() => {
                setText(chosenBody);
                setSaved(false);
              }}
            >
              {filled ? "Replace with template" : "Insert template"}
            </button>
            {!chosenBody && chosen && (
              <span className="field-hint">{noTemplateReason}</span>
            )}
          </div>
        )}

        <label className="form-grid__full">
          <textarea
            className="sg-ci-dockerfile"
            rows={18}
            spellCheck={false}
            value={text}
            placeholder={
              templateByType[ownType]?.dockerfile ||
              "FROM registry.example.com/openjdk:11-jre-slim\nWORKDIR /app\nADD app.jar app.jar\nENTRYPOINT [\"java\",\"-jar\",\"/app/app.jar\"]"
            }
            disabled={!canEdit}
            onChange={(event) => {
              setText(event.target.value);
              setSaved(false);
            }}
          />
          <span className="field-hint">
            The build context is the checkout, so <code>COPY target/*.jar</code> refers
            to a file an earlier stage produced there.
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
