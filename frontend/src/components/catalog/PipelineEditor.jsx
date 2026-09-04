import { useEffect, useState } from "react";
import {
  applyCiPipelineTemplate,
  listCiPipelines,
  listCiSecrets,
  updateCiPipeline,
} from "../../api/ciApi.js";
import LoadingState from "../common/LoadingState.jsx";
import PipelineStrip from "./PipelineStrip.jsx";
import {
  CONDITIONAL_STAGE_TYPES,
  DownIcon,
  PlusIcon,
  RUNNER_TYPES,
  STAGE_TYPES,
  TrashIcon,
  UNIMPLEMENTED_STAGE_TYPES,
  UpIcon,
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

/**
 * Pipeline tab: the ordered stage list plus one expanded stage editor.
 *
 * The whole pipeline saves in one request, which is what makes reordering a
 * local array move rather than a sequence of API calls that can half-apply.
 */
export default function PipelineEditor({ service, onChanged, canEdit }) {
  const [pipeline, setPipeline] = useState(null);
  const [stages, setStages] = useState([]);
  const [secretKeys, setSecretKeys] = useState([]);
  const [openIndex, setOpenIndex] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const data = await listCiPipelines(service.id);
      const first = data.items?.[0] || null;
      setPipeline(first);
      setStages(first?.stages ? first.stages.map((stage) => ({ ...stage })) : []);
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
    setOpenIndex(openIndex === index ? target : openIndex === target ? index : openIndex);
    setDirty(true);
  };

  const remove = (index) => {
    setStages((prev) => prev.filter((_, position) => position !== index));
    setOpenIndex(null);
    setDirty(true);
  };

  const add = () => {
    setStages((prev) => [...prev, blankStage()]);
    setOpenIndex(stages.length);
    setDirty(true);
  };

  const save = async () => {
    if (!pipeline) return;
    setSaving(true);
    setError("");
    try {
      const saved = await updateCiPipeline(pipeline.id, {
        name: pipeline.name,
        stages: stages.map((stage) => ({
          ...stage,
          timeoutSeconds: Number(stage.timeoutSeconds) || 1800,
        })),
      });
      setPipeline(saved);
      setStages(saved.stages.map((stage) => ({ ...stage })));
      setDirty(false);
      onChanged?.();
    } catch (err) {
      setError(err.message || "Could not save the pipeline.");
    } finally {
      setSaving(false);
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
      setDirty(false);
      onChanged?.();
    } catch (err) {
      setError(err.message || "Could not apply the template.");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <LoadingState label="Loading pipeline…" />;

  return (
    <div className="sg-ci-panel">
      {error && <p className="banner-message error">{error}</p>}

      {stages.length > 0 && <PipelineStrip stages={stages} />}

      <div className="sg-ci-panel-actions">
        {canEdit && (
          <>
            <button type="button" className="btn-outline btn-compact" onClick={add}>
              <PlusIcon /> Add stage
            </button>
            <button
              type="button"
              className="btn-outline btn-compact"
              onClick={resetToTemplate}
              disabled={saving}
            >
              Reset to template
            </button>
            {/* Quiet when there is nothing to save — a disabled primary reads
                as a broken button, not as a state. */}
            <button
              type="button"
              className={dirty ? "primary btn-compact" : "btn-outline btn-compact"}
              onClick={save}
              disabled={saving || !dirty}
            >
              {saving ? "Saving…" : dirty ? "Save pipeline" : "Saved ✓"}
            </button>
          </>
        )}
        {pipeline && (
          <span className="muted sg-ci-panel-note">
            {pipeline.name} · revision {pipeline.version}
          </span>
        )}
      </div>

      {stages.length === 0 ? (
        <p className="muted">
          This pipeline has no stages yet.{" "}
          {canEdit ? "Add one, or reset to the starter template." : ""}
        </p>
      ) : (
        <ol className="sg-ci-stage-editor">
          {stages.map((stage, index) => (
            <li key={index} className="sg-ci-stage-card">
              <div className="sg-ci-stage-card-head">
                <button
                  type="button"
                  className="sg-ci-stage-card-toggle"
                  onClick={() => setOpenIndex(openIndex === index ? null : index)}
                  aria-expanded={openIndex === index}
                >
                  <span className="sg-ci-stage-index">{index + 1}</span>
                  <span className="sg-ci-stage-title">
                    {stage.name || <em className="muted">Unnamed stage</em>}
                  </span>
                  <span className="chip">{stage.stageType}</span>
                  {UNIMPLEMENTED_STAGE_TYPES.has(stage.stageType) && (
                    <span className="status-pill warn">
                      {stage.stageType === "container_image" ? "needs BuildKit" : "no executor yet"}
                    </span>
                  )}
                  {stage.continueOnFailure && <span className="chip">continues on failure</span>}
                </button>
                {canEdit && (
                  <div className="sg-ci-stage-card-actions">
                    <button
                      type="button"
                      className="icon-button"
                      aria-label="Move up"
                      disabled={index === 0}
                      onClick={() => move(index, -1)}
                    >
                      <UpIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-button"
                      aria-label="Move down"
                      disabled={index === stages.length - 1}
                      onClick={() => move(index, 1)}
                    >
                      <DownIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-button danger"
                      aria-label="Remove stage"
                      onClick={() => remove(index)}
                    >
                      <TrashIcon />
                    </button>
                  </div>
                )}
              </div>

              {openIndex === index && (
                <StageFields
                  stage={stage}
                  secretKeys={secretKeys}
                  canEdit={canEdit}
                  onChange={(patch) => mutate(index, patch)}
                />
              )}
            </li>
          ))}
        </ol>
      )}
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
  checkout: new Set(["runner"]),
  command: new Set(["runner", "image", "workdir", "commands", "env", "secrets", "artifacts"]),
  container_image: new Set(["runner", "workdir", "env"]),
  publish_artifact: new Set([]),
  scan: new Set([]),
};

// Everything the new type will not use, cleared on the way — otherwise a value
// typed under one type lingers invisibly in the saved pipeline.
const CLEARED_BY_FIELD = {
  image: { image: "" },
  workdir: { workingDirectory: "" },
  commands: { commands: [] },
  env: { env: {} },
  secrets: { secretRefs: [] },
  artifacts: { artifacts: [] },
};

function StageFields({ stage, secretKeys, canEdit, onChange }) {
  const unimplemented = UNIMPLEMENTED_STAGE_TYPES.has(stage.stageType);
  const fields = STAGE_FIELDS[stage.stageType] || STAGE_FIELDS.command;
  const shows = (field) => fields.has(field);

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
            <textarea
              rows={4}
              style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
              value={toLines(stage.commands)}
              placeholder={"mvn -B clean package\nmvn -B test"}
              disabled={!canEdit}
              onChange={(event) => onChange({ commands: fromLines(event.target.value) })}
            />
            <span className="field-hint">
              One per line. Never put a secret here — reference it below instead.
            </span>
          </label>
        )}

        {shows("env") && (
          <label className="form-grid__full">
            Environment
            <textarea
              rows={3}
              style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
              value={envToText(stage.env)}
              placeholder={
                stage.stageType === "container_image"
                  ? "IMAGE_NAME=profile-ms\nIMAGE_TAG=V1.0.27\nDOCKERFILE_PATH=Dockerfile"
                  : "MAVEN_OPTS=-Xmx2g"
              }
              disabled={!canEdit}
              onChange={(event) => onChange({ env: envFromText(event.target.value) })}
            />
            <span className="field-hint">
              {stage.stageType === "container_image"
                ? "KEY=value, one per line. IMAGE_NAME, IMAGE_TAG and DOCKERFILE_PATH override the defaults (service slug, git ref, Dockerfile)."
                : "KEY=value, one per line. Not for secrets."}
            </span>
          </label>
        )}

        {shows("secrets") && (
        <div className="form-grid__full">
          <p className="form-label">Secrets</p>
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
        )}

        {shows("artifacts") && (
        <label className="form-grid__full">
          Artifacts to collect
          <textarea
            rows={2}
            style={{ resize: "vertical", fontFamily: "var(--font-mono, monospace)" }}
            value={(stage.artifacts || [])
              .map((item) => `${item.path}:${item.type || "binary"}`)
              .join("\n")}
            placeholder={"target/*.jar:jar\ntarget/surefire-reports/*.xml:test-report"}
            disabled={!canEdit}
            onChange={(event) =>
              onChange({
                artifacts: fromLines(event.target.value).map((line) => {
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
        )}

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
    </div>
  );
}
