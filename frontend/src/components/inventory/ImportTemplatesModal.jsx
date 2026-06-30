import { useEffect, useRef, useState } from "react";

import { createWizardTemplate, importTemplatesFromYaml } from "../../api/inventoryApi.js";
import CreateTemplateModal from "./CreateTemplateModal.jsx";

/**
 * Upload (or paste) Kubernetes YAML, parse each workload into a template draft,
 * then review + save each draft through the existing CreateTemplateModal. Pure
 * import front-end onto the UserTemplate system — saving uses the normal create
 * endpoint, so imported templates behave exactly like hand-authored ones.
 */
export default function ImportTemplatesModal({
  open,
  existingCategories = [],
  onClose,
  onSaved,
}) {
  const [yamlText, setYamlText] = useState("");
  const [fileName, setFileName] = useState("");
  const [drafts, setDrafts] = useState([]);
  const [parsing, setParsing] = useState(false);
  const [parseError, setParseError] = useState("");
  const [savedIndexes, setSavedIndexes] = useState(() => new Set());
  const [savedAny, setSavedAny] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const fileInputRef = useRef(null);

  useEffect(() => {
    if (open) {
      setYamlText("");
      setFileName("");
      setDrafts([]);
      setParsing(false);
      setParseError("");
      setSavedIndexes(new Set());
      setSavedAny(false);
      setActiveIndex(-1);
    }
  }, [open]);

  if (!open) return null;

  const handleClose = () => {
    if (savedAny) onSaved?.();
    onClose?.();
  };

  const handleFile = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => setYamlText(String(reader.result || ""));
    reader.onerror = () => setParseError("Could not read the selected file.");
    reader.readAsText(file);
  };

  const handleParse = async () => {
    if (!yamlText.trim()) {
      setParseError("Provide a YAML file or paste YAML to parse.");
      return;
    }
    setParsing(true);
    setParseError("");
    setDrafts([]);
    setSavedIndexes(new Set());
    try {
      const data = await importTemplatesFromYaml(yamlText);
      const found = data?.drafts || [];
      setDrafts(found);
      if (!found.length) setParseError("No importable workloads were found.");
    } catch (err) {
      setParseError(err.message || "Failed to parse YAML.");
    } finally {
      setParsing(false);
    }
  };

  // The draft category should appear in the modal's category dropdown.
  const categoriesWithDrafts = Array.from(
    new Set([...(existingCategories || []), ...drafts.map((d) => d.category || "Imported")]),
  );

  const activeDraft = activeIndex >= 0 ? drafts[activeIndex] : null;

  const handleSaveDraft = async (payload) => {
    await createWizardTemplate(payload);
    setSavedIndexes((prev) => new Set(prev).add(activeIndex));
    setSavedAny(true);
    setActiveIndex(-1);
  };

  return (
    <>
      <div className="modal-backdrop" role="presentation" onClick={handleClose}>
        <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
          <div className="modal-card__header">
            <h3>Import Templates from YAML</h3>
            <p className="muted">
              Upload or paste Kubernetes YAML. Each Deployment (or other workload) becomes a draft
              you can review — choosing what a deployer may change vs. what stays locked — and save
              as a reusable template.
            </p>
          </div>

          {parseError ? <p className="banner-message error">{parseError}</p> : null}

          <section className="form-section">
            <h4>Source</h4>
            <div className="form-grid">
              <label className="form-grid__full">
                YAML file
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".yaml,.yml,text/yaml,application/x-yaml"
                  onChange={handleFile}
                />
              </label>
              <label className="form-grid__full">
                …or paste YAML{fileName ? ` (loaded ${fileName})` : ""}
                <textarea
                  value={yamlText}
                  onChange={(e) => setYamlText(e.target.value)}
                  placeholder="apiVersion: apps/v1&#10;kind: Deployment&#10;..."
                  rows={10}
                  spellCheck={false}
                  style={{ fontFamily: "var(--font-mono, monospace)", resize: "vertical" }}
                />
              </label>
            </div>
            <button type="button" className="primary" onClick={handleParse} disabled={parsing}>
              {parsing ? "Parsing…" : "Parse YAML"}
            </button>
          </section>

          {drafts.length ? (
            <section className="form-section">
              <h4>Found workloads ({drafts.length})</h4>
              <div className="schema-env-list">
                {drafts.map((draft, index) => {
                  const container = draft.containers?.[0] || {};
                  const saved = savedIndexes.has(index);
                  const image = container.image
                    ? `${container.image}${container.tag ? `:${container.tag}` : ""}`
                    : "—";
                  return (
                    <div key={index} className="schema-env-card">
                      <div className="schema-env-card__top">
                        <div style={{ flex: 1 }}>
                          <strong>{draft.name}</strong>{" "}
                          <span className="template-card__badge">{draft.workloadType}</span>
                          <div className="muted" style={{ fontSize: "0.85em" }}>
                            {image}
                            {draft.scaling?.replicas != null ? ` · ${draft.scaling.replicas} replica(s)` : ""}
                          </div>
                        </div>
                        <button
                          type="button"
                          className={saved ? "btn-outline" : "primary"}
                          onClick={() => setActiveIndex(index)}
                          disabled={!container.image}
                        >
                          {saved ? "✓ Saved — Edit again" : "Configure & Save"}
                        </button>
                      </div>
                      {(draft.warnings || []).length ? (
                        <ul className="muted" style={{ margin: "var(--space-2) 0 0", paddingLeft: "1.2em" }}>
                          {draft.warnings.map((w, i) => (
                            <li key={i}>⚠️ {w}</li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </section>
          ) : null}

          <div className="modal-actions">
            <button type="button" className="btn-outline" onClick={handleClose}>
              {savedAny ? "Done" : "Close"}
            </button>
          </div>
        </div>
      </div>

      {activeDraft ? (
        <CreateTemplateModal
          open={Boolean(activeDraft)}
          draft={activeDraft}
          existingCategories={categoriesWithDrafts}
          onClose={() => setActiveIndex(-1)}
          onSubmit={handleSaveDraft}
        />
      ) : null}
    </>
  );
}
