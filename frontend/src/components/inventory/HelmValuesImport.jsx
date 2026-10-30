import { useEffect, useState } from "react";

import { saveHelmValuesFile } from "../../api/helmApi.js";
import { confirmDialog } from "../../lib/dialogs.js";
import YamlFileButton from "../common/YamlFileButton.jsx";

const PREVIEW_PATHS = 6;

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function PathList({ paths, total }) {
  const shown = paths.slice(0, PREVIEW_PATHS);
  return (
    <>
      {shown.map((path, index) => (
        <span key={path}>
          {index ? ", " : " "}
          <code>{path}</code>
        </span>
      ))}
      {total > shown.length ? <span className="muted"> and {total - shown.length} more</span> : null}
    </>
  );
}

/** A suggested file name from the imported one: "prod-values.yaml" → "prod". */
function suggestedName(fileName) {
  const stem = String(fileName || "")
    .toLowerCase()
    .replace(/\.ya?ml$/, "")
    .replace(/^values[-_.]?|[-_.]?values$/g, "")
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .slice(0, 40);
  return stem && stem !== "pasted" ? stem : "";
}

/**
 * Import a values.yaml into the chart deploy form: the modal fills every field
 * it can (secrets included) and sends the whole file along, so values without a
 * field apply too. Optionally stores it on the chart as values-<name>.yaml with
 * its secrets blanked.
 */
export default function HelmValuesImport({
  templateId,
  version,
  imported,
  values = {},
  busy,
  canSave,
  onApply,
  onClear,
  onSaved,
}) {
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [saveName, setSaveName] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setSaveName(suggestedName(imported?.name));
    setSaved(null);
    setError("");
  }, [imported?.text, imported?.name]);

  const apply = async (text, name) => {
    setError("");
    try {
      await onApply(text, name);
      setPasteOpen(false);
      setPasteText("");
    } catch (err) {
      setError(err.message || "The values.yaml could not be imported.");
    }
  };

  const save = async (replace = false) => {
    setSaving(true);
    setError("");
    try {
      const result = await saveHelmValuesFile(templateId, {
        name: saveName,
        version,
        valuesYaml: imported.text,
        replace,
      });
      setSaved({ path: result.savedValuesFile, blanked: (result.secretsBlanked || []).length });
      onSaved?.(result);
    } catch (err) {
      if (err.status === 409 && !replace) {
        setSaving(false);
        const confirmed = await confirmDialog({
          title: "Replace values file",
          message: `${err.message} Replace it with the imported file? Its secrets are blanked either way.`,
          confirmLabel: "Replace",
        });
        if (confirmed) await save(true);
        return;
      }
      setError(err.message || "The values file could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  if (!imported) {
    return (
      <section className="helm-values-import">
        <div className="helm-values-import-head">
          <div>
            <strong>Have a values.yaml?</strong>
            <small className="form-help">
              Import it to fill every field below, secrets included. Values with no field here
              are applied as written.
            </small>
          </div>
          <div className="helm-values-import-actions">
            <YamlFileButton disabled={busy} onLoad={apply} onError={setError} />
            <button
              type="button"
              className="btn-text btn-sm"
              aria-expanded={pasteOpen}
              onClick={() => setPasteOpen((open) => !open)}
            >
              Paste instead
            </button>
          </div>
        </div>
        {pasteOpen ? (
          <div className="helm-values-import-paste">
            <textarea
              className="yaml-editor"
              rows={8}
              spellCheck={false}
              value={pasteText}
              placeholder={"replicaCount: 2\ndatabase:\n  password: ..."}
              aria-label="values.yaml to import"
              onChange={(event) => setPasteText(event.target.value)}
            />
            <button
              type="button"
              className="btn-outline btn-sm"
              disabled={busy || !pasteText.trim()}
              onClick={() => apply(pasteText, "pasted values.yaml")}
            >
              Fill the form
            </button>
          </div>
        ) : null}
        {error ? <p className="banner-message error">{error}</p> : null}
      </section>
    );
  }

  const { report } = imported;
  const filled = Object.keys(report.fields || {}).length;
  // Required values the file left empty, minus the ones typed in since.
  const stillEmpty = (report.missingRequired || []).filter(
    (item) => String(values[item.path] ?? "").trim() === "",
  );
  return (
    <section className="helm-values-import is-applied" aria-live="polite">
      <div className="helm-values-import-head">
        <div>
          <strong>Imported {imported.name}</strong>
          <small className="form-help">
            {plural(report.valueCount, "value")}, layered over the chart defaults
            {report.valuesFile ? ` and ${report.valuesFile}` : ""}. Fields changed below still win.
          </small>
        </div>
        <div className="helm-values-import-actions">
          <YamlFileButton className="btn-text btn-sm" disabled={busy} onLoad={apply} onError={setError}>
            Use another file
          </YamlFileButton>
          <button type="button" className="btn-text btn-sm" disabled={busy} onClick={onClear}>
            Remove
          </button>
        </div>
      </div>

      <ul className="helm-import-summary">
        <li className="is-ok">
          Filled {plural(filled, "field")}
          {report.secretCount ? `, with ${plural(report.secretCount, "secret")} from the file` : ""}.
        </li>
        {report.withoutFieldCount ? (
          <li>
            {plural(report.withoutFieldCount, "value")} {report.withoutFieldCount === 1 ? "has" : "have"} no
            field here and {report.withoutFieldCount === 1 ? "is" : "are"} applied as written:
            <PathList paths={report.withoutField} total={report.withoutFieldCount} />
          </li>
        ) : null}
        {report.unknownCount ? (
          <li className="is-warn">
            {plural(report.unknownCount, "key")} {report.unknownCount === 1 ? "is" : "are"} not in this
            chart&apos;s values.yaml. {report.unknownCount === 1 ? "It is" : "They are"} still applied, but
            the chart may ignore {report.unknownCount === 1 ? "it" : "them"}; check the spelling:
            <PathList paths={report.unknown} total={report.unknownCount} />
          </li>
        ) : null}
        {stillEmpty.length ? (
          <li className="is-error">
            Not in the file, fill {stillEmpty.length === 1 ? "it" : "them"} in below:{" "}
            {stillEmpty.map((item) => item.label).join(", ")}
          </li>
        ) : (
          <li className="is-ok">Every required value is filled.</li>
        )}
      </ul>

      {canSave ? (
        saved ? (
          <p className="banner-message" role="status">
            Saved as {saved.path} on v{version}. Pick it under Environment values next time
            {saved.blanked
              ? `; its ${plural(saved.blanked, "secret")} ${saved.blanked === 1 ? "was" : "were"} blanked and will be asked for again.`
              : "."}
          </p>
        ) : (
          // Not a <form>: this sits inside the deploy form, and Enter here must
          // save the file rather than start the dry run.
          <div className="helm-values-import-save">
            <label>
              <span>Keep it on this chart (v{version}) as</span>
              <span className="helm-values-file-name">
                values-
                <input
                  maxLength={40}
                  value={saveName}
                  placeholder="prod"
                  aria-label="Values file name"
                  onChange={(event) =>
                    setSaveName(event.target.value.toLowerCase().replace(/[^a-z0-9._-]/g, ""))
                  }
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    if (saveName && !saving) save();
                  }}
                />
                .yaml
              </span>
            </label>
            <button
              type="button"
              className="btn-outline btn-sm"
              disabled={saving || busy || !saveName}
              onClick={() => save()}
            >
              {saving ? "Saving…" : "Save values file"}
            </button>
            <small className="form-help">
              Secrets are blanked in the saved file: the form asks for them again when you pick it.
            </small>
          </div>
        )
      ) : null}
      {error ? <p className="banner-message error">{error}</p> : null}
    </section>
  );
}
