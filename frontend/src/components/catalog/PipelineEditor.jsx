import { useEffect, useRef, useState } from "react";
import BuildParameters from "./BuildParameters.jsx";
import JenkinsfileImportModal from "./JenkinsfileImportModal.jsx";
import {
  applyCiPipelineTemplate,
  createCiPipeline,
  lintCiPipeline,
  listCiPipelines,
  listCiSecrets,
  updateCiPipeline,
} from "../../api/ciApi.js";
import LoadingState from "../common/LoadingState.jsx";
import {
  CheckIcon,
  CONDITIONAL_STAGE_TYPES,
  DEFAULT_IMAGE_SCAN,
  DownIcon,
  IMAGE_SCAN_ON_FAIL,
  IMAGE_SCAN_THRESHOLDS,
  PlusIcon,
  RUNNER_TYPES,
  STAGE_TYPES,
  TrashIcon,
  UNIMPLEMENTED_STAGE_TYPES,
  UpIcon,
  applicationTypeLabel,
} from "./ciShared.jsx";

const blankStage = () => ({
  name: "",
  stageType: "command",
  runnerType: "",
  runnerLabels: [],
  image: "",
  workingDirectory: "",
  commands: [],
  env: {},
  secretRefs: [],
  artifacts: [],
  hostAliases: [],
  runCondition: null,
  // Null, not a default object: a new stage is a command stage, and only an
  // image stage has an image to gate.
  imageScan: null,
  timeoutSeconds: 1800,
  continueOnFailure: false,
  enabled: true,
});

const toLines = (values) => (values || []).join("\n");
const fromLines = (text) =>
  String(text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

// ip=host,host per line, mirroring how the backend accepts and validates them.
const aliasesToText = (aliases) =>
  (aliases || [])
    .map((entry) => `${entry.ip}=${(entry.hostnames || []).join(",")}`)
    .join("\n");

// Parsed leniently here — the backend is the validator, so a half-typed line
// does not throw while somebody is still typing it.
const aliasesFromText = (text) =>
  fromLines(text)
    .map((line) => {
      const index = line.indexOf("=");
      if (index <= 0) return null;
      const hostnames = line
        .slice(index + 1)
        .split(",")
        .map((name) => name.trim())
        .filter(Boolean);
      return { ip: line.slice(0, index).trim(), hostnames };
    })
    .filter(Boolean);

const envToText = (env) =>
  Object.entries(env || {})
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");

const envFromText = (text) => {
  const out = {};
  fromLines(text).forEach((line) => {
    const index = line.indexOf("=");
    if (index > 0) out[line.slice(0, index).trim()] = line.slice(index + 1);
  });
  return out;
};

// A stage with no condition always runs; the summary says so rather than
// leaving the row blank, because "no condition" and "condition not yet filled
// in" look identical otherwise.
const conditionSummary = (condition) => {
  if (!condition?.variable) return "Always";
  const verb = condition.operator === "not_equals" ? "is not" : "is";
  return `${condition.variable} ${verb} "${condition.value ?? ""}"`;
};

/** Armed = a scan actually gates this stage's push. Absent and
 * `{enabled: false}` are both "not armed", but they are different answers to
 * "was this image scanned?" and the panel below shows them differently. */
const scanArmed = (stage) =>
  Boolean(stage.imageScan && stage.imageScan.enabled !== false);

const stageTypeLabel = (stageType) =>
  STAGE_TYPES.find((type) => type.value === stageType)?.label || stageType;

const stageSummary = (stage) => {
  if (stage.stageType === "checkout") return "Repository source";
  if (stage.stageType === "container_image") {
    const where = stage.workingDirectory || "Dockerfile from service root";
    return scanArmed(stage) ? `${where} · scanned before push` : where;
  }
  if (stage.stageType === "publish_artifact") {
    const count = (stage.artifacts || []).length;
    return count ? `${count} artifact ${count === 1 ? "pattern" : "patterns"}` : "Artifact handoff";
  }
  if (stage.stageType === "scan") return "Security policy scan";

  const parts = [];
  if (stage.image) parts.push(stage.image);
  if ((stage.runnerLabels || []).length) parts.push(stage.runnerLabels.join(" + "));
  if ((stage.commands || []).length) {
    const count = stage.commands.filter(Boolean).length;
    parts.push(`${count} ${count === 1 ? "command" : "commands"}`);
  }
  return parts.join(" · ") || "Not configured";
};

/**
 * A textarea whose stored form cannot represent everything a person types.
 *
 * Commands are stored as an array and env as an object, so re-serialising on
 * every keystroke deletes the blank line you just made with Enter — the value
 * snaps back and the key appears dead. Hold the raw text while the field has
 * focus, publish the parsed form as you type so nothing is lost on save, and
 * re-sync to the canonical text on blur.
 */
function DraftTextarea({ value, onChangeText, ...props }) {
  const [draft, setDraft] = useState(value);
  const focused = useRef(false);

  useEffect(() => {
    if (!focused.current) setDraft(value);
  }, [value]);

  return (
    <textarea
      {...props}
      value={draft}
      onFocus={() => {
        focused.current = true;
      }}
      onBlur={() => {
        focused.current = false;
        setDraft(value);
      }}
      onChange={(event) => {
        setDraft(event.target.value);
        onChangeText(event.target.value);
      }}
    />
  );
}

/**
 * Pipeline tab: the ordered stage list plus one expanded stage editor.
 *
 * The whole pipeline saves in one request, which is what makes reordering a
 * local array move rather than a sequence of API calls that can half-apply.
 */
export default function PipelineEditor({ service, onChanged, canEdit }) {
  const [pipeline, setPipeline] = useState(null);
  const [stages, setStages] = useState([]);
  const [parameters, setParameters] = useState([]);
  const [secretKeys, setSecretKeys] = useState([]);
  const [selectedIndex, setSelectedIndex] = useState(null);
  const [activePanel, setActivePanel] = useState("stage");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  // What these stages assume about the runner they land on. Checked as they are
  // edited, because the failure it catches — an absolute /workspace path on an
  // agent — is only visible when a build has already burned.
  const [lint, setLint] = useState(null);
  const [importing, setImporting] = useState(false);
  // What the Jenkinsfile could not carry over, kept after the dialog closes:
  // the list is the to-do for finishing the port, and it is only actionable
  // next to the stages it is about.
  const [importNotes, setImportNotes] = useState(null);
  const generated = Boolean(pipeline?.isGeneratedDefault);
  const editable = canEdit && !generated;

  const load = async () => {
    setLoading(true);
    try {
      const data = await listCiPipelines(service.id);
      const first = data.items?.[0] || null;
      const nextStages = first?.stages
        ? first.stages.map((stage) => ({ ...stage }))
        : [];
      setPipeline(first);
      setStages(nextStages);
      setParameters(first?.parameters ? first.parameters.map((item) => ({ ...item })) : []);
      setSelectedIndex(nextStages.length ? 0 : null);
      setActivePanel(nextStages.length ? "stage" : "parameters");
      setDirty(false);
      setError("");
    } catch (err) {
      setError(err.message || "Could not load the pipeline.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    listCiSecrets(service.id)
      .then((data) => setSecretKeys((data.items || []).map((item) => item.key)))
      .catch(() => setSecretKeys([]));
  }, [service.id]);

  const mutate = (index, patch) => {
    setStages((prev) =>
      prev.map((stage, position) =>
        position === index ? { ...stage, ...patch } : stage
      )
    );
    setDirty(true);
  };

  const move = (index, delta) => {
    const target = index + delta;
    if (target < 0 || target >= stages.length) return;
    setStages((prev) => {
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    setSelectedIndex((current) =>
      current === index ? target : current === target ? index : current
    );
    setActivePanel("stage");
    setDirty(true);
  };

  const remove = (index) => {
    setStages((prev) => prev.filter((_, position) => position !== index));
    setSelectedIndex((current) => {
      if (stages.length <= 1) return null;
      if (current === index) return Math.min(index, stages.length - 2);
      return current > index ? current - 1 : current;
    });
    if (stages.length <= 1) setActivePanel("parameters");
    setDirty(true);
  };

  const add = () => {
    setStages((prev) => [...prev, blankStage()]);
    setSelectedIndex(stages.length);
    setActivePanel("stage");
    setDirty(true);
  };

  // Debounced: this runs while somebody types a command, and the answer is
  // only interesting once they stop. A failed check is silent — a linter that
  // shouts about its own outage is worse than no linter.
  useEffect(() => {
    if (!stages.length) {
      setLint(null);
      return undefined;
    }
    const timer = window.setTimeout(() => {
      lintCiPipeline(stages)
        .then(setLint)
        .catch(() => setLint(null));
    }, 600);
    return () => window.clearTimeout(timer);
  }, [stages]);

  const save = async () => {
    if (!pipeline) return;
    setSaving(true);
    setError("");
    try {
      const payload = {
        name: pipeline.name,
        isDefault: true,
        parameters,
        stages: stages.map((stage) => ({
          ...stage,
          timeoutSeconds: Number(stage.timeoutSeconds) || 1800,
        })),
      };
      const saved = pipeline.id
        ? await updateCiPipeline(pipeline.id, payload)
        : await createCiPipeline(service.id, payload);
      setPipeline(saved);
      setStages(saved.stages.map((stage) => ({ ...stage })));
      setParameters((saved.parameters || []).map((item) => ({ ...item })));
      setSelectedIndex((current) =>
        saved.stages.length ? Math.min(current ?? 0, saved.stages.length - 1) : null
      );
      setDirty(false);
      onChanged?.();
    } catch (err) {
      setError(err.message || "Could not save the pipeline.");
    } finally {
      setSaving(false);
    }
  };

  const customizeDefault = () => {
    setPipeline((current) => ({
      ...current,
      isGeneratedDefault: false,
      version: current?.version || 1,
    }));
    setDirty(true);
    if (!stages.length) {
      setStages([blankStage()]);
      setSelectedIndex(0);
      setActivePanel("stage");
    }
  };

  const resetToTemplate = async () => {
    if (
      !window.confirm(
        "Replace every stage with the starter pipeline for this application type?"
      )
    )
      return;
    setSaving(true);
    setError("");
    try {
      const saved = await applyCiPipelineTemplate(service.id, service.applicationType);
      setPipeline(saved);
      setStages(saved.stages.map((stage) => ({ ...stage })));
      setSelectedIndex(saved.stages.length ? 0 : null);
      setActivePanel(saved.stages.length ? "stage" : "parameters");
      setDirty(false);
      onChanged?.();
    } catch (err) {
      setError(err.message || "Could not apply the template.");
    } finally {
      setSaving(false);
    }
  };

  /**
   * Take a draft from the Jenkinsfile importer.
   *
   * Local state only, and deliberately: the draft becomes unsaved changes that
   * are reviewed and saved with the same button as any other edit. A working
   * pipeline is never replaced by a translation nobody read.
   */
  const applyDraft = (draft) => {
    setPipeline((current) => ({
      ...(current || { name: "default", id: null }),
      isGeneratedDefault: false,
    }));
    setStages(draft.stages.map((stage) => ({ ...stage })));
    setParameters(draft.parameters.map((item) => ({ ...item })));
    setImportNotes(draft.notes?.length || draft.blocking?.length ? draft : null);
    setSelectedIndex(draft.stages.length ? 0 : null);
    setActivePanel(draft.stages.length ? "stage" : "parameters");
    setDirty(true);
    setImporting(false);
  };

  if (loading) return <LoadingState label="Loading pipeline…" />;

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      {importing && (
        <JenkinsfileImportModal
          service={service}
          onApply={applyDraft}
          onClose={() => setImporting(false)}
        />
      )}

      {generated && (
        <section className="sg-ci-default-pipeline" aria-label="KubeSight default pipeline">
          <div>
            <strong>Using KubeSight default pipeline</strong>
            <span>
              Application type: {pipeline.defaultMetadata?.applicationTypeLabel ||
                applicationTypeLabel(service.applicationType)}
            </span>
            <span>
              Detected command: <code>{pipeline.defaultMetadata?.detectedCommand}</code>
            </span>
            {pipeline.defaultMetadata?.detectedFiles?.length > 0 && (
              <small>
                Detected: {pipeline.defaultMetadata.detectedFiles.join(", ")}
              </small>
            )}
          </div>
          {canEdit && (
            <button type="button" className="primary btn-compact" onClick={customizeDefault}>
              Customize Pipeline
            </button>
          )}
        </section>
      )}

      {/* Survives the dialog: what a Jenkinsfile could not carry is work to do
          in this editor, and it has to still be readable while it is done. */}
      {importNotes && (
        <div className="sg-ci-import-carryover">
          <div className="sg-ci-import-carryover-head">
            <strong>Imported from a Jenkinsfile — {importNotes.summary}</strong>
            {/* "Dismiss", not a trash icon: the notes are a reminder, and an
                icon that reads as delete makes people keep a list they have
                already dealt with. */}
            <button
              type="button"
              className="btn-outline btn-compact"
              onClick={() => setImportNotes(null)}
            >
              Dismiss
            </button>
          </div>
          {importNotes.blocking?.length > 0 && (
            <ul className="sg-ci-import-blocking">
              {importNotes.blocking.map((message, index) => (
                <li key={index}>{message}</li>
              ))}
            </ul>
          )}
          <ul className="sg-ci-import-notes">
            {importNotes.notes.map((note, index) => (
              <li key={index} className={`is-${note.level}`}>
                {note.stage ? (
                  <button
                    type="button"
                    className="sg-ci-lint-stage"
                    onClick={() => {
                      const position = stages.findIndex(
                        (stage) => stage.name === note.stage
                      );
                      if (position >= 0) {
                        setSelectedIndex(position);
                        setActivePanel("stage");
                      }
                    }}
                  >
                    {note.stage}
                  </button>
                ) : (
                  <span className="sg-ci-import-note-stage">Pipeline</span>
                )}
                <span>{note.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {lint && lint.counts.error + lint.counts.warning > 0 && (
        <div
          className={`sg-ci-lint${lint.counts.error ? " is-error" : ""}`}
          aria-live="polite"
        >
          <p className="sg-ci-lint-summary">{lint.summary}</p>
          <ul>
            {lint.findings
              .filter((finding) => finding.level !== "info")
              .map((finding) => (
                <li key={`${finding.stagePosition}-${finding.code}`}>
                  <button
                    type="button"
                    className="sg-ci-lint-stage"
                    onClick={() => {
                      const index = stages.findIndex(
                        (stage, position) =>
                          (stage.position ?? position + 1) === finding.stagePosition
                      );
                      if (index >= 0) {
                        setSelectedIndex(index);
                        setActivePanel("stage");
                      }
                    }}
                  >
                    {finding.stageName}
                  </button>
                  <span className={`sg-ci-lint-level is-${finding.level}`}>
                    {finding.level === "error" ? "fails" : "differs"}
                    {finding.breaksOn === "agent"
                      ? " on an agent"
                      : finding.breaksOn === "kubernetes"
                        ? " on Kubernetes"
                        : ""}
                  </span>
                  <span>{finding.message}</span>
                  {finding.fix && <em className="muted"> {finding.fix}</em>}
                </li>
              ))}
          </ul>
        </div>
      )}

      <div className="sg-ci-pipeline-bar">
        {pipeline && (
          <div className="sg-ci-pipeline-identity">
            <strong>{pipeline.name}</strong>
            <span>{generated ? "Generated for each build" : `Revision ${pipeline.version}`}</span>
          </div>
        )}
        <span className={`status-pill ${stages.length ? "ok" : "warn"} sg-ci-pipeline-ready`}>
          {stages.length > 0 && <CheckIcon />}
          {stages.length > 0
            ? `${stages.length} ${stages.length === 1 ? "stage" : "stages"} configured`
            : "No stages configured"}
        </span>
        <span className={`sg-ci-save-state${dirty ? " is-dirty" : ""}`} aria-live="polite">
          {generated ? "Managed by KubeSight" : dirty ? "Unsaved changes" : "All changes saved"}
        </span>
        {canEdit && (
          <div className="sg-ci-pipeline-actions">
            <button
              type="button"
              className="btn-outline btn-compact"
              onClick={() => setImporting(true)}
              disabled={saving}
              title="Read a Jenkinsfile into these stages and build inputs"
            >
              Import Jenkinsfile
            </button>
            {!generated && (
              <button
                type="button"
                className="btn-outline btn-compact"
                onClick={resetToTemplate}
                disabled={saving}
              >
                Reset to template
              </button>
            )}
            {/* Quiet when there is nothing to save — a disabled primary reads
                as a broken button, not as a state. */}
            <button
              type="button"
              className={dirty ? "primary btn-compact" : "btn-outline btn-compact"}
              onClick={save}
              disabled={saving || !dirty || generated}
            >
              {saving ? "Saving…" : dirty ? "Save pipeline" : "Saved ✓"}
            </button>
          </div>
        )}
      </div>

      <div className="sg-ci-pipeline-workspace">
        <aside className="sg-ci-pipeline-rail" aria-label="Pipeline structure">
          <div className="sg-ci-pipeline-rail-head">
            <div>
              <strong>Pipeline stages</strong>
              <span>{stages.length}</span>
            </div>
            <small>Select to edit</small>
          </div>

          {stages.length > 0 ? (
            <ol className="sg-ci-stage-rail-list">
              {stages.map((stage, index) => (
                <li key={index}>
                  <button
                    type="button"
                    className={`sg-ci-stage-rail-item${
                      activePanel === "stage" && selectedIndex === index ? " is-active" : ""
                    }`}
                    onClick={() => {
                      setSelectedIndex(index);
                      setActivePanel("stage");
                    }}
                    aria-current={
                      activePanel === "stage" && selectedIndex === index ? "step" : undefined
                    }
                  >
                    <span className="sg-ci-stage-index">{index + 1}</span>
                    <span className="sg-ci-stage-rail-copy">
                      <strong>{stage.name || <em>Unnamed stage</em>}</strong>
                      <small>{stageSummary(stage)}</small>
                    </span>
                    <span className="sg-ci-stage-kind">{stageTypeLabel(stage.stageType)}</span>
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <div className="sg-ci-stage-rail-empty">
              <strong>No stages yet</strong>
              <span>Add a stage or restore the starter template.</span>
            </div>
          )}

          {editable && (
            <button type="button" className="btn-outline btn-compact sg-ci-add-stage" onClick={add}>
              <PlusIcon /> Add stage
            </button>
          )}

          <button
            type="button"
            className={`sg-ci-pipeline-section-link${activePanel === "parameters" ? " is-active" : ""}`}
            onClick={() => setActivePanel("parameters")}
          >
            <span>
              <strong>Build inputs</strong>
              <small>Shown before a manual run</small>
            </span>
            <span className="sg-ci-section-count">{parameters.length} configured</span>
          </button>
        </aside>

        <section className="sg-ci-pipeline-inspector">
          {activePanel === "parameters" ? (
            <BuildParameters
              parameters={parameters}
              canEdit={editable}
              onChange={(next) => {
                setParameters(next);
                setDirty(true);
              }}
            />
          ) : selectedIndex !== null && stages[selectedIndex] ? (
            <>
              <header className="sg-ci-inspector-head">
                <div>
                  <span className="sg-ci-inspector-kicker">
                    Stage {selectedIndex + 1} of {stages.length} ·{" "}
                    {stageTypeLabel(stages[selectedIndex].stageType)}
                  </span>
                  <h3>{stages[selectedIndex].name || "Unnamed stage"}</h3>
                  <p>{stageSummary(stages[selectedIndex])}</p>
                </div>
                {editable && (
                  <div className="sg-ci-inspector-actions">
                    <button
                      type="button"
                      className="icon-button"
                      aria-label="Move stage up"
                      title="Move stage up"
                      disabled={selectedIndex === 0}
                      onClick={() => move(selectedIndex, -1)}
                    >
                      <UpIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-button"
                      aria-label="Move stage down"
                      title="Move stage down"
                      disabled={selectedIndex === stages.length - 1}
                      onClick={() => move(selectedIndex, 1)}
                    >
                      <DownIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-button danger"
                      aria-label="Remove stage"
                      title="Remove stage"
                      onClick={() => remove(selectedIndex)}
                    >
                      <TrashIcon />
                    </button>
                  </div>
                )}
              </header>
              <StageFields
                stage={stages[selectedIndex]}
                secretKeys={secretKeys}
                parameters={parameters}
                canEdit={editable}
                onChange={(patch) => mutate(selectedIndex, patch)}
              />
            </>
          ) : (
            <div className="sg-ci-pipeline-empty">
              <strong>This pipeline has no stages yet.</strong>
              <p>
                {editable
                  ? "Add one, or reset to the starter template."
                  : "There is nothing to configure."}
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

/**
 * Which fields a stage type actually consumes at run time. Offering the rest
 * is a lie the runner then ignores: a checkout runs a fixed script in the
 * worker image, so its `image` and `commands` go nowhere, and artifacts are
 * explicitly skipped for container_image stages (the image IS the artifact).
 * Keep this in step with runners/kubernetes.py.
 */
const STAGE_FIELDS = {
  // hostAliases is offered wherever a stage reaches the network — a checkout
  // clones from a git host, so it needs name resolution too.
  checkout: new Set(["runner", "hostAliases"]),
  command: new Set([
    "runner",
    "image",
    "workdir",
    "hostAliases",
    "commands",
    "env",
    "secrets",
    "artifacts",
  ]),
  container_image: new Set(["runner", "workdir", "hostAliases", "env", "imageScan"]),
  publish_artifact: new Set([]),
  scan: new Set([]),
};

// Everything the new type will not use, cleared on the way — otherwise a value
// typed under one type lingers invisibly in the saved pipeline.
const CLEARED_BY_FIELD = {
  hostAliases: { hostAliases: [] },
  image: { image: "" },
  workdir: { workingDirectory: "" },
  commands: { commands: [] },
  env: { env: {} },
  secrets: { secretRefs: [] },
  artifacts: { artifacts: [] },
  // Cleared when the stage stops being an image stage: a gate left behind on a
  // command stage would be rejected on save, and would read as protection that
  // is not there until then.
  imageScan: { imageScan: null },
};

function StageFields({ stage, secretKeys, parameters, canEdit, onChange }) {
  const unimplemented = UNIMPLEMENTED_STAGE_TYPES.has(stage.stageType);
  const fields = STAGE_FIELDS[stage.stageType] || STAGE_FIELDS.command;
  const shows = (field) => fields.has(field);

  const condition = stage.runCondition || { variable: "", operator: "equals", value: "" };
  // A condition saved against a parameter that was later renamed still has to
  // be selectable, or opening the editor would silently drop it on save.
  const declared = (parameters || []).map((item) => item.name).filter(Boolean);
  const conditionChoices = condition.variable && !declared.includes(condition.variable)
    ? [...declared, condition.variable]
    : declared;

  const changeType = (stageType) => {
    const next = STAGE_FIELDS[stageType] || STAGE_FIELDS.command;
    const patch = { stageType };
    for (const [field, cleared] of Object.entries(CLEARED_BY_FIELD)) {
      if (!next.has(field)) Object.assign(patch, cleared);
    }
    onChange(patch);
  };

  return (
    <div className="sg-ci-stage-card-body">
      {unimplemented && (
        <p className="banner-message info">{CONDITIONAL_STAGE_TYPES[stage.stageType]}</p>
      )}

      {stage.stageType === "checkout" && (
        <p className="muted sg-ci-stage-note">
          Clones the repository into <code>/workspace/source</code> using a fixed script.
          The repository, branch and credentials come from the Source tab — there is
          nothing to configure here.
        </p>
      )}

      <div className="form-grid">
        <label>
          Stage name *
          <input
            value={stage.name}
            maxLength={120}
            disabled={!canEdit}
            onChange={(event) => onChange({ name: event.target.value })}
          />
        </label>
        <label>
          Type
          <select
            value={stage.stageType}
            disabled={!canEdit}
            onChange={(event) => changeType(event.target.value)}
          >
            {STAGE_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
        </label>

        {shows("runner") && (
          <>
            <label>
              Runner
              <select
                value={stage.runnerType || ""}
                disabled={!canEdit}
                onChange={(event) => onChange({ runnerType: event.target.value })}
              >
                {RUNNER_TYPES.map((type) => (
                  <option key={type.value} value={type.value}>
                    {type.label}
                  </option>
                ))}
              </select>
              <span className="field-hint">Leave as "any" and let labels decide.</span>
            </label>
            <label>
              Required capabilities
              <input
                value={(stage.runnerLabels || []).join(", ")}
                placeholder="linux, java21"
                disabled={!canEdit}
                onChange={(event) =>
                  onChange({
                    runnerLabels: event.target.value
                      .split(",")
                      .map((item) => item.trim().toLowerCase())
                      .filter(Boolean),
                  })
                }
              />
              <span className="field-hint">
                A runner must advertise all of these to be eligible.
              </span>
            </label>
          </>
        )}

        {shows("image") && (
          <label>
            Container image
            <input
              value={stage.image || ""}
              placeholder="maven:3.9-eclipse-temurin-21"
              disabled={!canEdit}
              onChange={(event) => onChange({ image: event.target.value })}
            />
          </label>
        )}
        {shows("workdir") && (
          <label>
            Working directory
            <input
              value={stage.workingDirectory || ""}
              placeholder="(service default)"
              disabled={!canEdit}
              onChange={(event) => onChange({ workingDirectory: event.target.value })}
            />
            {stage.stageType === "container_image" && (
              <span className="field-hint">
                The build context — where the Dockerfile is looked for.
              </span>
            )}
          </label>
        )}

        {shows("commands") && (
          <label className="form-grid__full">
            Commands
            <DraftTextarea
              rows={8}
              style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
              value={toLines(stage.commands)}
              placeholder={"mvn -B clean package\nmvn -B test"}
              disabled={!canEdit}
              spellCheck={false}
              // Lines are kept verbatim: these join back into one shell script,
              // where a heredoc's blank lines and indentation are content.
              onChangeText={(text) => onChange({ commands: text.split("\n") })}
            />
            <span className="field-hint">
              One per line. Never put a secret here — reference it below instead.
              Commands run in the checkout; use <code>$KUBESIGHT_WORKSPACE</code> and{" "}
              <code>$KUBESIGHT_SOURCE</code> for absolute paths, since a literal{" "}
              <code>/workspace</code> exists only on the Kubernetes runner.
            </span>
          </label>
        )}

        {shows("hostAliases") && (
          <details className="sg-ci-stage-options form-grid__full">
            <summary>
              <span>
                <strong>Runtime &amp; networking</strong>
                <small>Host aliases used by the build container</small>
              </span>
              <span className="sg-ci-option-value">
                {(stage.hostAliases || []).length
                  ? `${stage.hostAliases.length} configured`
                  : "Optional"}
              </span>
            </summary>
            <div className="sg-ci-stage-options-body">
              <label>
                Host aliases
                <DraftTextarea
                  rows={3}
                  style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
                  value={aliasesToText(stage.hostAliases)}
                  placeholder={"10.10.10.20=nexus.areeba.com,nexus\n10.10.10.30=db.internal"}
                  disabled={!canEdit}
                  spellCheck={false}
                  onChangeText={(text) => onChange({ hostAliases: aliasesFromText(text) })}
                />
                <span className="field-hint">
                  One per line as <code>ip=hostname</code>. Separate several hostnames for
                  one address with commas.
                  {stage.stageType === "container_image" && (
                    <>
                      {" "}These reach Dockerfile <code>RUN</code> steps, but not the base
                      image pull or result push handled by BuildKit.
                    </>
                  )}
                </span>
              </label>
            </div>
          </details>
        )}

        {shows("imageScan") && (
          <details className="sg-ci-stage-options form-grid__full" open={scanArmed(stage)}>
            <summary>
              <span>
                <strong>Image scan</strong>
                <small>Checked between building the image and pushing it</small>
              </span>
              <span className="sg-ci-option-value">
                {scanArmed(stage)
                  ? `Blocks at ${
                      IMAGE_SCAN_THRESHOLDS.find(
                        (item) => item.value === (stage.imageScan.threshold || "critical")
                      )?.label || stage.imageScan.threshold
                    }`
                  : stage.imageScan
                    ? "Turned off"
                    : "Not configured"}
              </span>
            </summary>
            <div className="sg-ci-stage-options-body">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={scanArmed(stage)}
                  disabled={!canEdit}
                  onChange={(event) =>
                    onChange({
                      imageScan: event.target.checked
                        ? { ...DEFAULT_IMAGE_SCAN, ...(stage.imageScan || {}), enabled: true }
                        : { ...DEFAULT_IMAGE_SCAN, ...(stage.imageScan || {}), enabled: false },
                    })
                  }
                />
                <span>Scan this image before pushing it</span>
              </label>
              <span className="field-hint form-grid__full">
                BuildKit stops short of the registry: the image it built is scanned
                here, and pushed only if it passes. Nothing reaches the registry
                first, so there is no vulnerable tag to clean up afterwards. Builds
                already running keep the pipeline they started with — this applies
                from the next build.
              </span>

              {scanArmed(stage) && (
                <>
                  <label>
                    Block on
                    <select
                      value={stage.imageScan.threshold || "critical"}
                      disabled={!canEdit}
                      onChange={(event) =>
                        onChange({
                          imageScan: { ...stage.imageScan, threshold: event.target.value },
                        })
                      }
                    >
                      {IMAGE_SCAN_THRESHOLDS.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                    <span className="field-hint">
                      Every severity is recorded in the report either way. This only
                      decides which ones stop the push. High is routinely non-empty
                      on a stock base image.
                    </span>
                  </label>

                  <label>
                    When something is found
                    <select
                      value={stage.imageScan.onFail || "block"}
                      disabled={!canEdit}
                      onChange={(event) =>
                        onChange({
                          imageScan: { ...stage.imageScan, onFail: event.target.value },
                        })
                      }
                    >
                      {IMAGE_SCAN_ON_FAIL.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                    <span className="field-hint">
                      {(stage.imageScan.onFail || "block") === "block"
                        ? "The stage fails and the image is not pushed."
                        : "The report is attached and the image is pushed anyway — a gate that only reports."}
                    </span>
                  </label>

                  <label className="checkbox-row form-grid__full">
                    <input
                      type="checkbox"
                      checked={Boolean(stage.imageScan.ignoreUnfixed)}
                      disabled={!canEdit}
                      onChange={(event) =>
                        onChange({
                          imageScan: {
                            ...stage.imageScan,
                            ignoreUnfixed: event.target.checked,
                          },
                        })
                      }
                    />
                    <span>Ignore findings with no fix available</span>
                  </label>
                  <span className="field-hint form-grid__full">
                    A CVE with no released fix cannot be cleared by rebuilding, so
                    counting it blocks a build nobody can unblock. Off by default —
                    ignoring them is a policy choice, not KubeSight's to make.
                  </span>

                  <p className="banner-message info form-grid__full">
                    Needs the KubeSight CI image tools (buildctl + Trivy + crane) on
                    the cluster: build <code>Dockerfile.ci-imagetools</code> and
                    mirror it. Until it is there, a build of this stage fails to
                    start and says so — it never falls back to pushing unscanned.
                  </p>
                </>
              )}
            </div>
          </details>
        )}

        {shows("env") && (
          <details className="sg-ci-stage-options form-grid__full">
            <summary>
              <span>
                <strong>Environment</strong>
                <small>Plain runtime values</small>
              </span>
              <span className="sg-ci-option-value">
                {Object.keys(stage.env || {}).length
                  ? `${Object.keys(stage.env).length} configured`
                  : "Optional"}
              </span>
            </summary>
            <div className="sg-ci-stage-options-body">
              <label>
                Environment variables
                <DraftTextarea
              rows={4}
              style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
              value={envToText(stage.env)}
              placeholder={
                stage.stageType === "container_image"
                  ? "IMAGE_NAME=profile-ms\nIMAGE_TAG=V1.0.27\nDOCKERFILE_PATH=Dockerfile"
                  : "MAVEN_OPTS=-Xmx2g"
              }
              disabled={!canEdit}
              onChangeText={(text) => onChange({ env: envFromText(text) })}
                />
                <span className="field-hint">
                  {stage.stageType === "container_image"
                    ? "KEY=value, one per line. IMAGE_NAME, IMAGE_TAG and DOCKERFILE_PATH override the defaults (service slug, git ref, Dockerfile)."
                    : "KEY=value, one per line. Not for secrets."}
                </span>
              </label>
            </div>
          </details>
        )}

        {shows("secrets") && (
          <details className="sg-ci-stage-options form-grid__full">
            <summary>
              <span>
                <strong>Secrets</strong>
                <small>Protected values injected as environment variables</small>
              </span>
              <span className="sg-ci-option-value">
                {(stage.secretRefs || []).length
                  ? `${stage.secretRefs.length} selected`
                  : "None selected"}
              </span>
            </summary>
            <div className="sg-ci-stage-options-body">
              {secretKeys.length === 0 ? (
                <p className="muted">
                  No secrets are defined for this service yet. Add them on the Settings tab.
                </p>
              ) : (
                <div className="sg-ci-secret-refs">
                  {secretKeys.map((key) => {
                    const ref = (stage.secretRefs || []).find((item) => item.name === key);
                    return (
                      <label key={key} className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={Boolean(ref)}
                          disabled={!canEdit}
                          onChange={(event) =>
                            onChange({
                              secretRefs: event.target.checked
                                ? [...(stage.secretRefs || []), { name: key, envVar: key }]
                                : (stage.secretRefs || []).filter((item) => item.name !== key),
                            })
                          }
                        />
                        <code>{key}</code>
                        {ref && (
                          <input
                            className="sg-ci-envvar-input"
                            value={ref.envVar}
                            aria-label={`Environment variable for ${key}`}
                            disabled={!canEdit}
                            onChange={(event) =>
                              onChange({
                                secretRefs: (stage.secretRefs || []).map((item) =>
                                  item.name === key
                                    ? { ...item, envVar: event.target.value }
                                    : item
                                ),
                              })
                            }
                          />
                        )}
                      </label>
                    );
                  })}
                </div>
              )}
            </div>
          </details>
        )}

        {shows("artifacts") && (
          <details className="sg-ci-stage-options form-grid__full">
            <summary>
              <span>
                <strong>Artifacts</strong>
                <small>Files collected after this stage finishes</small>
              </span>
              <span className="sg-ci-option-value">
                {(stage.artifacts || []).length
                  ? `${stage.artifacts.length} patterns`
                  : "None"}
              </span>
            </summary>
            <div className="sg-ci-stage-options-body">
              <label>
                Artifacts to collect
                <DraftTextarea
                  rows={3}
                  style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
                  value={(stage.artifacts || [])
                    .map((item) => `${item.path}:${item.type || "binary"}`)
                    .join("\n")}
                  placeholder={"target/*.jar:jar\ntarget/surefire-reports/*.xml:test-report"}
                  disabled={!canEdit}
                  spellCheck={false}
                  onChangeText={(text) =>
                    onChange({
                      artifacts: fromLines(text).map((line) => {
                        const index = line.lastIndexOf(":");
                        return index > 0
                          ? { path: line.slice(0, index), type: line.slice(index + 1) }
                          : { path: line, type: "binary" };
                      }),
                    })
                  }
                />
                <span className="field-hint">One per line, as path:type.</span>
              </label>
            </div>
          </details>
        )}

        <details className="sg-ci-stage-options form-grid__full">
          <summary>
            <span>
              <strong>Runs when</strong>
              <small>Skip this stage unless a build input says otherwise</small>
            </span>
            <span className="sg-ci-option-value">{conditionSummary(stage.runCondition)}</span>
          </summary>
          <div className="sg-ci-stage-options-body form-grid">
            <label>
              Build input
              <select
                value={condition.variable}
                disabled={!canEdit}
                onChange={(event) =>
                  onChange({
                    runCondition: event.target.value
                      ? { ...condition, variable: event.target.value }
                      : null,
                  })
                }
              >
                <option value="">Always runs</option>
                {conditionChoices.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <span className="field-hint">
                {(parameters || []).length
                  ? "One of this pipeline's build inputs."
                  : "This pipeline has no build inputs yet — add one under Build inputs first."}
              </span>
            </label>
            <label>
              Comparison
              <select
                value={condition.operator}
                disabled={!canEdit || !condition.variable}
                onChange={(event) =>
                  onChange({ runCondition: { ...condition, operator: event.target.value } })
                }
              >
                <option value="equals">is</option>
                <option value="not_equals">is not</option>
              </select>
            </label>
            <label>
              Value
              <input
                value={condition.value}
                placeholder="true"
                disabled={!canEdit || !condition.variable}
                onChange={(event) =>
                  onChange({ runCondition: { ...condition, value: event.target.value } })
                }
              />
              <span className="field-hint">
                Compared as text. A yes/no input holds <code>true</code> or <code>false</code>.
              </span>
            </label>
          </div>
        </details>

        <details className="sg-ci-stage-options form-grid__full">
          <summary>
            <span>
              <strong>Failure behavior</strong>
              <small>Timeout and what happens after an error</small>
            </span>
            <span className="sg-ci-option-value">
              {stage.continueOnFailure ? "Continue pipeline" : "Stop pipeline"}
            </span>
          </summary>
          <div className="sg-ci-stage-options-body form-grid">
            <label>
              Timeout (seconds)
              <input
                type="number"
                min={30}
                max={86400}
                value={stage.timeoutSeconds}
                disabled={!canEdit}
                onChange={(event) => onChange({ timeoutSeconds: event.target.value })}
              />
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={Boolean(stage.continueOnFailure)}
                disabled={!canEdit}
                onChange={(event) => onChange({ continueOnFailure: event.target.checked })}
              />
              Continue if this stage fails
              <span className="field-hint">
                Later stages still run, but the build is still reported as failed.
              </span>
            </label>
          </div>
        </details>
      </div>
    </div>
  );
}
