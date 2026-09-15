import { useEffect, useRef, useState } from "react";
import { importCiJenkinsfile, readCiSourceFile } from "../../api/ciApi.js";
import { CheckIcon } from "./ciShared.jsx";

/**
 * Import a Jenkinsfile as a pipeline draft.
 *
 * The shape of this dialog is the argument for the whole feature: read on the
 * left, review on the right, and nothing is written until the editor's own Save
 * is pressed. A translation that landed straight in the database would be a
 * pipeline nobody had read — the reason the backend returns a draft rather than
 * saving one.
 *
 * Three ways in, because a Jenkinsfile lives in three places: in the repository
 * this service already points at, on disk, or in a clipboard.
 */

const LEVEL_LABEL = {
  error: "Needs you",
  warning: "Check",
  info: "Changed",
};

const stageSummary = (stage) => {
  const parts = [];
  if (stage.stageType !== "command") parts.push(stage.stageType.replace("_", " "));
  if (stage.image) parts.push(stage.image);
  if (stage.workingDirectory) parts.push(`in ${stage.workingDirectory}`);
  const count = (stage.commands || []).filter(Boolean).length;
  if (count) parts.push(`${count} ${count === 1 ? "line" : "lines"}`);
  return parts.join(" · ") || "Nothing to run";
};

const conditionSummary = (condition) => {
  if (!condition?.variable) return "";
  const verb = condition.operator === "not_equals" ? "is not" : "is";
  return `only when ${condition.variable} ${verb} "${condition.value ?? ""}"`;
};

const typeLabel = (parameter) => {
  if (parameter.type === "dynamic_choice") return `from the repository (${parameter.source})`;
  if (parameter.type === "choice") return (parameter.choices || []).join(" / ");
  if (parameter.type === "multiline") return "text block";
  if (parameter.type === "boolean") return `yes / no · ${parameter.default}`;
  return parameter.default ? `text · ${parameter.default}` : "text";
};

export default function JenkinsfileImportModal({ service, onApply, onClose }) {
  const [content, setContent] = useState("");
  const [draft, setDraft] = useState(null);
  const [reading, setReading] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState("");
  const [path, setPath] = useState("Jenkinsfile");
  const fileInput = useRef(null);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Re-reading on every keystroke would be a request per character for no gain:
  // a Jenkinsfile arrives pasted or loaded, whole.
  const read = async (text) => {
    if (!text.trim()) {
      setError("Paste a Jenkinsfile, or load one, to import it.");
      return;
    }
    setReading(true);
    setError("");
    try {
      setDraft(await importCiJenkinsfile(text, service.id));
    } catch (err) {
      setDraft(null);
      setError(err.message || "That could not be read as a declarative Jenkinsfile.");
    } finally {
      setReading(false);
    }
  };

  const loadFromRepository = async () => {
    setFetching(true);
    setError("");
    try {
      const file = await readCiSourceFile(service.id, path.trim() || "Jenkinsfile");
      setContent(file.content);
      await read(file.content);
    } catch (err) {
      setError(err.message || "That file could not be read from the repository.");
    } finally {
      setFetching(false);
    }
  };

  const loadFromDisk = (file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      setContent(text);
      read(text);
    };
    reader.onerror = () => setError("That file could not be read.");
    reader.readAsText(file);
  };

  const blocked = (draft?.blocking || []).length > 0;

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card sg-ci-import"
        role="dialog"
        aria-label="Import a Jenkinsfile"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>Import a Jenkinsfile</h3>
          <p className="muted">
            Reads the stages, their commands, and the build inputs with their names and
            types. Nothing is saved — the draft lands in the editor for you to finish and
            save yourself.
          </p>
        </div>

        <div className="sg-ci-import-body">
          <section className="sg-ci-import-source">
            <div className="sg-ci-import-sourcebar">
              <label className="field sg-ci-import-path">
                <span>Path in {service.repositoryName || "the repository"}</span>
                <input
                  type="text"
                  value={path}
                  onChange={(event) => setPath(event.target.value)}
                  placeholder="Jenkinsfile"
                  disabled={!service.sourceConfigured}
                />
              </label>
              <button
                type="button"
                className="btn-outline btn-compact"
                onClick={loadFromRepository}
                disabled={fetching || !service.sourceConfigured}
                title={
                  service.sourceConfigured
                    ? "Read this file from the connected repository"
                    : "Connect a repository on the Source tab first"
                }
              >
                {fetching ? "Reading…" : "Load from repository"}
              </button>
              <button
                type="button"
                className="btn-outline btn-compact"
                onClick={() => fileInput.current?.click()}
              >
                Upload a file
              </button>
              <input
                ref={fileInput}
                type="file"
                className="sg-ci-import-file"
                onChange={(event) => {
                  loadFromDisk(event.target.files?.[0]);
                  event.target.value = "";
                }}
              />
            </div>

            <textarea
              className="sg-ci-import-textarea"
              value={content}
              spellCheck={false}
              onChange={(event) => setContent(event.target.value)}
              placeholder={"pipeline {\n    agent any\n    stages {\n        …\n    }\n}"}
              aria-label="Jenkinsfile"
            />

            <div className="sg-ci-import-readbar">
              <button
                type="button"
                className="primary btn-compact"
                onClick={() => read(content)}
                disabled={reading || !content.trim()}
              >
                {reading ? "Reading…" : draft ? "Read again" : "Read it"}
              </button>
              {error && <span className="sg-ci-import-error">{error}</span>}
            </div>
          </section>

          <section className="sg-ci-import-review" aria-live="polite">
            {!draft ? (
              <div className="sg-ci-import-empty">
                <strong>Nothing read yet</strong>
                <p>
                  Load or paste a declarative Jenkinsfile — the one that starts{" "}
                  <code>pipeline {"{"}</code> — and what it translates to appears here.
                </p>
              </div>
            ) : (
              <>
                <p className="sg-ci-import-summary">{draft.summary}</p>

                {draft.parameters.length > 0 && (
                  <div className="sg-ci-import-group">
                    <h4>Build inputs</h4>
                    <ul className="sg-ci-import-params">
                      {draft.parameters.map((parameter) => (
                        <li key={parameter.name}>
                          <code>{parameter.name}</code>
                          <span>{typeLabel(parameter)}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="sg-ci-import-group">
                  <h4>Stages</h4>
                  <ol className="sg-ci-import-stages">
                    {draft.stages.map((stage, index) => (
                      <li key={`${stage.name}-${index}`}>
                        <span className="sg-ci-stage-index">{index + 1}</span>
                        <span className="sg-ci-import-stage-copy">
                          <strong>{stage.name}</strong>
                          <small>{stageSummary(stage)}</small>
                          {stage.runCondition && (
                            <em>{conditionSummary(stage.runCondition)}</em>
                          )}
                        </span>
                      </li>
                    ))}
                  </ol>
                </div>

                {draft.secrets.length > 0 && (
                  <div className="sg-ci-import-group">
                    <h4>Credentials it read</h4>
                    <ul className="sg-ci-import-secrets">
                      {draft.secrets.map((secret) => (
                        <li key={secret.name}>
                          <code>{secret.name}</code>
                          {secret.defined ? (
                            <span className="is-ready">
                              <CheckIcon /> already a secret here
                            </span>
                          ) : (
                            <span className="is-missing">
                              add it under Secrets{secret.credentialId
                                ? ` (Jenkins: ${secret.credentialId})`
                                : ""}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {draft.notes.length > 0 && (
                  <div className="sg-ci-import-group">
                    <h4>What did not come across</h4>
                    <ul className="sg-ci-import-notes">
                      {draft.notes.map((note, index) => (
                        <li key={index} className={`is-${note.level}`}>
                          <span className="sg-ci-import-note-level">
                            {LEVEL_LABEL[note.level] || note.level}
                          </span>
                          {note.stage && (
                            <span className="sg-ci-import-note-stage">{note.stage}</span>
                          )}
                          <span>{note.message}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </section>
        </div>

        <div className="modal-actions sg-ci-import-actions">
          {/* Said before the button, not after it: replacing the stage list is
              the part nobody can undo by pressing Escape. */}
          <span className="muted">
            {draft
              ? blocked
                ? "This draft cannot be saved as it is — the editor will show what to fix."
                : "Replaces the stages and build inputs in the editor. Nothing is saved until you press Save pipeline."
              : ""}
          </span>
          <button type="button" className="btn-outline" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            disabled={!draft}
            onClick={() => onApply(draft)}
          >
            Use this draft
          </button>
        </div>
      </div>
    </div>
  );
}
