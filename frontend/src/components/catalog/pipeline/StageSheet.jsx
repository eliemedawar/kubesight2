import { useEffect, useId, useRef } from "react";
import {
  CONDITIONAL_STAGE_TYPES,
  DEFAULT_IMAGE_SCAN,
  IMAGE_SCAN_ON_FAIL,
  IMAGE_SCAN_THRESHOLDS,
  RETIRED_STAGE_TYPES,
  RUNNER_TYPES,
} from "../ciShared.jsx";
import {
  AliasRows,
  ArtifactRows,
  ChipsInput,
  CommandEditor,
  EnvRows,
  Field,
  Segmented,
  SettingRow,
  Switch,
} from "./controls.jsx";
import { PlIcon } from "./icons.jsx";
import {
  conditionSummary,
  DEFAULT_TIMEOUT_SECONDS,
  fieldsFor,
  IMAGE_ENV_KEYS,
  kindOf,
  mergePlainEnv,
  plainEnv,
  scanArmed,
  STAGE_KINDS,
  stageSummary,
  timeoutLabel,
} from "./stageModel.js";

// Suggested images per application type. Suggestions only — any image the
// runner can pull works, and the field says so.
const IMAGE_SUGGESTIONS = {
  java_maven: ["maven:3.9-eclipse-temurin-21", "maven:3.9-eclipse-temurin-17"],
  java_gradle: ["gradle:8-jdk21", "gradle:8-jdk17"],
  java: ["eclipse-temurin:21-jdk", "maven:3.9-eclipse-temurin-21"],
  node: ["node:22-alpine", "node:20-alpine", "node:20"],
  python: ["python:3.12-slim", "python:3.11-slim"],
  android: ["cimg/android:2024.01"],
  flutter: ["ghcr.io/cirruslabs/flutter:stable"],
  container: ["alpine:3.20", "debian:bookworm-slim"],
  generic: ["alpine:3.20", "debian:bookworm-slim", "ubuntu:24.04"],
};

const TIMEOUT_PRESETS = [
  [600, "10 min"],
  [1800, "30 min"],
  [3600, "1 h"],
  [7200, "2 h"],
];

const FAILURE_LEVEL_TEXT = {
  error: "Fails",
  warning: "Check",
};

/**
 * One stage, top to bottom, in the order the questions come: what it does,
 * when it runs, what goes in and out, and where it runs.
 *
 * The closed rows below the first block are the stage's summary — each one
 * names its value, so a stage can be read without opening anything, and the
 * rows that differ from the default are the ones marked.
 */
export default function StageSheet({
  service,
  stage,
  index,
  total,
  stages,
  parameters,
  secretKeys,
  problems,
  change,
  editable,
  onChange,
  onChangeKind,
  onDuplicate,
  onMove,
  onRemove,
  onGoToTab,
  onOpenInputs,
  focusName,
}) {
  const ids = useId();
  const nameRef = useRef(null);
  // A stage just added lands with its name selected, ready to be typed over.
  useEffect(() => {
    if (focusName && nameRef.current) {
      nameRef.current.focus();
      nameRef.current.select();
    }
  }, [focusName]);
  const kind = kindOf(stage.stageType);
  const retired = RETIRED_STAGE_TYPES.some((type) => type.value === stage.stageType);
  const fields = fieldsFor(stage.stageType);
  const has = (field) => fields.has(field);
  const enabled = stage.enabled !== false;
  const problemFor = (field) =>
    problems.find((item) => item.field === field && item.level !== "warning")?.message;

  const kindOptions = [
    ...STAGE_KINDS.map((item) => ({
      value: item.value,
      label: item.verb,
      hint: item.label,
      icon: item.icon,
    })),
    ...RETIRED_STAGE_TYPES.filter((type) => type.value === stage.stageType).map((type) => ({
      value: type.value,
      label: type.label,
      icon: "alert",
      disabled: true,
    })),
  ];

  return (
    <article
      className={`pl-sheet kind-${kind?.value || "retired"}${enabled ? "" : " is-off"}`}
      aria-labelledby={`${ids}-name`}
    >
      <header className="pl-sheet-head">
        <span className="pl-sheet-glyph" aria-hidden="true">
          <PlIcon name={kind?.icon || "alert"} />
        </span>
        <div className="pl-sheet-title">
          <span className="pl-kicker">
            Stage {index + 1} of {total}
            <span aria-hidden="true"> · </span>
            {kind?.label || "Unsupported"}
            {change && (
              <span className={`pl-flow-change is-${change}`}>
                {change === "new" ? "New — not saved yet" : "Edited"}
              </span>
            )}
          </span>
          {editable ? (
            <input
              ref={nameRef}
              id={`${ids}-name`}
              className={`pl-sheet-name${problemFor("name") ? " is-invalid" : ""}`}
              value={stage.name || ""}
              maxLength={120}
              placeholder="Name this stage"
              aria-label="Stage name"
              aria-invalid={Boolean(problemFor("name")) || undefined}
              onChange={(event) => onChange({ name: event.target.value })}
            />
          ) : (
            <h3 id={`${ids}-name`} className="pl-sheet-name is-static">
              {stage.name || "Unnamed stage"}
            </h3>
          )}
          <p className={`pl-sheet-summary${stage.stageType === "command" ? " is-mono" : ""}`}>{stageSummary(stage)}</p>
        </div>
        <div className="pl-sheet-tools">
          <Switch
            checked={enabled}
            disabled={!editable}
            label={enabled ? "On" : "Off"}
            onChange={(next) => onChange({ enabled: next })}
          />
          {editable && (
            <div className="pl-toolgroup" role="group" aria-label="Stage actions">
              <button
                type="button"
                className="btn-ghost pl-tool"
                onClick={onDuplicate}
                title="Duplicate stage"
                aria-label="Duplicate stage"
              >
                <PlIcon name="copy" />
              </button>
              <button
                type="button"
                className="btn-ghost pl-tool"
                onClick={() => onMove(-1)}
                disabled={index === 0}
                title="Move up (Alt+↑ in the flow)"
                aria-label="Move stage up"
              >
                <PlIcon name="up" />
              </button>
              <button
                type="button"
                className="btn-ghost pl-tool"
                onClick={() => onMove(1)}
                disabled={index === total - 1}
                title="Move down (Alt+↓ in the flow)"
                aria-label="Move stage down"
              >
                <PlIcon name="down" />
              </button>
              <button
                type="button"
                className="btn-ghost pl-tool is-danger"
                onClick={onRemove}
                title="Remove stage"
                aria-label="Remove stage"
              >
                <PlIcon name="trash" />
              </button>
            </div>
          )}
        </div>
      </header>

      {!enabled && (
        <div className="pl-note is-muted">
          <PlIcon name="power" />
          <p>
            <strong>This stage is off.</strong> Every build skips it until it is turned back on —
            nothing else about it changes, so it can be parked instead of deleted.
          </p>
        </div>
      )}

      {problems.length > 0 && (
        <div className="pl-problems" role="group" aria-label="Problems with this stage">
          {problems.map((problem, position) => (
            <div key={position} className={`pl-problem is-${problem.level || "error"}`}>
              <PlIcon name="alert" />
              <div>
                <p>
                  {problem.source === "lint" && (
                    <span className="pl-problem-tag">
                      {FAILURE_LEVEL_TEXT[problem.level] || "Check"}
                      {problem.breaksOn === "agent"
                        ? " on an agent"
                        : problem.breaksOn === "kubernetes"
                          ? " on Kubernetes"
                          : ""}
                    </span>
                  )}
                  {problem.source === "import" && <span className="pl-problem-tag">From the Jenkinsfile</span>}
                  {problem.message}
                </p>
                {problem.fix && <p className="pl-problem-fix">{problem.fix}</p>}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── What it does ─────────────────────────────────────────────── */}
      <section className="pl-block" aria-labelledby={`${ids}-what`}>
        <h4 id={`${ids}-what`} className="pl-block-title">
          What this stage does
        </h4>
        <Segmented
          label="Stage kind"
          size="lg"
          value={stage.stageType}
          options={kindOptions}
          disabled={!editable}
          onChange={onChangeKind}
        />

        {retired && (
          <div className="pl-note is-warn">
            <PlIcon name="alert" />
            <p>{CONDITIONAL_STAGE_TYPES[stage.stageType]}</p>
          </div>
        )}

        {stage.stageType === "checkout" && (
          <div className="pl-source-card">
            <PlIcon name="source" />
            <div>
              <strong className={service.sourceConfigured ? "is-mono" : undefined}>
                {service.sourceConfigured
                  ? `${service.repositoryWorkspace}/${service.repositoryName}`
                  : "No repository connected yet"}
              </strong>
              <p>
                Cloned into <code>$KUBESIGH_SOURCE</code> at the branch or tag the build was
                started for. The repository, default branch and credentials live on the Source
                tab — there is nothing to set here.
              </p>
            </div>
            <button type="button" className="btn-outline btn-compact" onClick={() => onGoToTab?.("source")}>
              Open Source
            </button>
          </div>
        )}

        {stage.stageType === "command" && (
          <>
            <div className="pl-grid">
              <Field
                label="Container image"
                htmlFor={`${ids}-image`}
                optional
                hint={
                  stage.image
                    ? "Pulled by the runner for this stage only."
                    : "Empty uses the runner's default: debian:bookworm-slim on Kubernetes (unless an admin changed it), the machine itself on an agent."
                }
              >
                <input
                  id={`${ids}-image`}
                  className="is-mono"
                  value={stage.image || ""}
                  list={`${ids}-images`}
                  placeholder={(IMAGE_SUGGESTIONS[service.applicationType] || IMAGE_SUGGESTIONS.generic)[0]}
                  disabled={!editable}
                  spellCheck={false}
                  onChange={(event) => onChange({ image: event.target.value })}
                />
                <datalist id={`${ids}-images`}>
                  {(IMAGE_SUGGESTIONS[service.applicationType] || IMAGE_SUGGESTIONS.generic).map((image) => (
                    <option key={image} value={image} />
                  ))}
                </datalist>
              </Field>
              <Field
                label="Working directory"
                htmlFor={`${ids}-workdir`}
                optional
                hint="Relative to the repository. Empty is the service's own directory."
              >
                <input
                  id={`${ids}-workdir`}
                  className="is-mono"
                  value={stage.workingDirectory || ""}
                  placeholder={service.workingDirectory || "."}
                  disabled={!editable}
                  spellCheck={false}
                  onChange={(event) => onChange({ workingDirectory: event.target.value })}
                />
              </Field>
            </div>
            <Field
              label="Commands"
              htmlFor={`${ids}-commands`}
              wide
              error={problemFor("commands")}
              hint={
                <>
                  Runs in the checkout. For absolute paths use <code>$KUBESIGH_WORKSPACE</code> and{" "}
                  <code>$KUBESIGH_SOURCE</code> — a literal <code>/workspace</code> exists only on
                  the Kubernetes runner. Never paste a secret here; attach it under Secrets.
                </>
              }
            >
              <CommandEditor
                id={`${ids}-commands`}
                lines={stage.commands}
                disabled={!editable}
                invalid={Boolean(problemFor("commands"))}
                placeholder={"mvn -B clean package\nmvn -B test"}
                onChange={(commands) => onChange({ commands })}
              />
            </Field>
          </>
        )}

        {stage.stageType === "container_image" && (
          <ImageStageFields
            ids={ids}
            service={service}
            stage={stage}
            editable={editable}
            onChange={onChange}
            onGoToTab={onGoToTab}
          />
        )}
      </section>

      {/* ── When it runs ─────────────────────────────────────────────── */}
      <section className="pl-block" aria-labelledby={`${ids}-when`}>
        <h4 id={`${ids}-when`} className="pl-block-title">
          When it runs
        </h4>
        <div className="pl-settings">
          <SettingRow
            icon="branch"
            title="Run condition"
            hint="Skip this stage unless a build input matches"
            value={conditionSummary(stage.runCondition)}
            isSet={Boolean(stage.runCondition?.variable)}
            tone={problems.some((item) => item.field === "condition") ? "warn" : undefined}
            defaultOpen={problems.some((item) => item.field === "condition")}
          >
            <ConditionBuilder
              stage={stage}
              parameters={parameters}
              editable={editable}
              onChange={onChange}
              onOpenInputs={onOpenInputs}
            />
          </SettingRow>
          <SettingRow
            icon="clock"
            title="If it fails or hangs"
            hint="What a failure does to the rest of the build"
            value={`${stage.continueOnFailure ? "Keep going" : "Stop the build"} · ${timeoutLabel(
              stage.timeoutSeconds
            )} limit`}
            isSet={
              Boolean(stage.continueOnFailure) ||
              Number(stage.timeoutSeconds || DEFAULT_TIMEOUT_SECONDS) !== DEFAULT_TIMEOUT_SECONDS
            }
            tone={problemFor("timeout") ? "error" : undefined}
            defaultOpen={Boolean(problemFor("timeout"))}
          >
            <FailureSettings ids={ids} stage={stage} editable={editable} onChange={onChange} error={problemFor("timeout")} />
          </SettingRow>
        </div>
      </section>

      {/* ── Inputs & outputs ─────────────────────────────────────────── */}
      {(has("env") || has("secrets") || has("artifacts")) && (
        <section className="pl-block" aria-labelledby={`${ids}-io`}>
          <h4 id={`${ids}-io`} className="pl-block-title">
            Inputs &amp; outputs
          </h4>
          <div className="pl-settings">
            {has("env") && (
              <SettingRow
                icon="variable"
                title="Variables"
                hint="Plain values set as environment variables"
                value={countLabel(Object.keys(plainEnv(stage)).length, "variable", "None")}
                isSet={Object.keys(plainEnv(stage)).length > 0}
              >
                <EnvRows
                  value={plainEnv(stage)}
                  disabled={!editable}
                  onChange={(next) => onChange({ env: mergePlainEnv(stage, next) })}
                />
                <p className="pl-field-hint">
                  Visible in the build log. Build inputs are passed to every stage as variables too
                  — a stage value here is overridden by a build input of the same name.
                </p>
              </SettingRow>
            )}
            {has("secrets") && (
              <SettingRow
                icon="key"
                title="Secrets"
                hint="Protected values, masked in logs"
                value={countLabel((stage.secretRefs || []).length, "secret", "None")}
                isSet={(stage.secretRefs || []).length > 0}
              >
                <SecretPicker
                  stage={stage}
                  secretKeys={secretKeys}
                  editable={editable}
                  onChange={onChange}
                  onGoToTab={onGoToTab}
                />
              </SettingRow>
            )}
            {has("artifacts") && (
              <SettingRow
                icon="file"
                title="Files to keep"
                hint="Collected as build artifacts when the stage ends"
                value={countLabel((stage.artifacts || []).length, "pattern", "Nothing kept")}
                isSet={(stage.artifacts || []).length > 0}
              >
                <ArtifactRows
                  value={stage.artifacts}
                  disabled={!editable}
                  onChange={(artifacts) => onChange({ artifacts })}
                />
              </SettingRow>
            )}
          </div>
        </section>
      )}

      {/* ── Where it runs ────────────────────────────────────────────── */}
      {(has("runner") || has("hostAliases")) && (
        <section className="pl-block" aria-labelledby={`${ids}-where`}>
          <h4 id={`${ids}-where`} className="pl-block-title">
            Where it runs
          </h4>
          <div className="pl-settings">
            {has("runner") && (
              <SettingRow
                icon="server"
                title="Runner"
                hint="Which machines may pick this stage up"
                value={runnerSummary(stage)}
                isSet={Boolean(stage.runnerType) || (stage.runnerLabels || []).length > 0}
              >
                <div className="pl-grid">
                  <Field label="Runner kind" htmlFor={`${ids}-runner`}>
                    <select
                      id={`${ids}-runner`}
                      value={stage.runnerType || ""}
                      disabled={!editable}
                      onChange={(event) => onChange({ runnerType: event.target.value })}
                    >
                      {RUNNER_TYPES.map((type) => (
                        <option key={type.value} value={type.value}>
                          {type.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field
                    label="Required capabilities"
                    hint="A runner must advertise every one. Press Enter or comma after each."
                  >
                    <ChipsInput
                      label="Required capabilities"
                      value={stage.runnerLabels}
                      placeholder="linux, java21"
                      disabled={!editable}
                      onChange={(runnerLabels) => onChange({ runnerLabels })}
                    />
                  </Field>
                </div>
              </SettingRow>
            )}
            {has("hostAliases") && (
              <SettingRow
                icon="network"
                title="Host aliases"
                hint="Extra name → IP entries for the build container"
                value={countLabel((stage.hostAliases || []).length, "alias", "None", "aliases")}
                isSet={(stage.hostAliases || []).length > 0}
              >
                <AliasRows
                  value={stage.hostAliases}
                  disabled={!editable}
                  onChange={(hostAliases) => onChange({ hostAliases })}
                />
                {stage.stageType === "container_image" && (
                  <p className="pl-field-hint">
                    These reach Dockerfile <code>RUN</code> steps, but not the base image pull or the
                    push, which BuildKit does itself.
                  </p>
                )}
              </SettingRow>
            )}
          </div>
        </section>
      )}
    </article>
  );
}

function countLabel(count, singular, none, plural) {
  if (!count) return none;
  return `${count} ${count === 1 ? singular : plural || `${singular}s`}`;
}

function runnerSummary(stage) {
  const type = RUNNER_TYPES.find((item) => item.value === (stage.runnerType || ""))?.label;
  const labels = stage.runnerLabels || [];
  return labels.length ? `${type} · ${labels.join(" + ")}` : type;
}

function ImageStageFields({ ids, service, stage, editable, onChange, onGoToTab }) {
  const env = stage.env || {};
  const setEnv = (key, value) => {
    const next = { ...env };
    if (value) next[key] = value;
    else delete next[key];
    onChange({ env: next });
  };
  const armed = scanArmed(stage);
  const scan = stage.imageScan || DEFAULT_IMAGE_SCAN;
  const setScan = (patch) =>
    onChange({ imageScan: { ...DEFAULT_IMAGE_SCAN, ...(stage.imageScan || {}), ...patch } });

  return (
    <>
      {!service.registryConnectionId && (
        <div className="pl-note is-warn">
          <PlIcon name="alert" />
          <p>
            <strong>No registry is linked to this service.</strong> A build skips this stage and
            says why until one is linked.{" "}
            <button type="button" className="btn-ghost pl-link" onClick={() => onGoToTab?.("settings")}>
              Link a registry in Settings
            </button>
          </p>
        </div>
      )}
      <div className="pl-grid">
        <Field
          label="Dockerfile"
          htmlFor={`${ids}-dockerfile`}
          optional
          hint={
            <>
              Path inside the build context. An inline Dockerfile saved on the{" "}
              <button type="button" className="btn-ghost pl-link" onClick={() => onGoToTab?.("dockerfile")}>
                Dockerfile tab
              </button>{" "}
              is used instead when there is one.
            </>
          }
        >
          <input
            id={`${ids}-dockerfile`}
            className="is-mono"
            value={env[IMAGE_ENV_KEYS.dockerfile] || ""}
            placeholder="Dockerfile"
            disabled={!editable}
            spellCheck={false}
            onChange={(event) => setEnv(IMAGE_ENV_KEYS.dockerfile, event.target.value)}
          />
        </Field>
        <Field
          label="Build context"
          htmlFor={`${ids}-context`}
          optional
          hint="The directory sent to BuildKit. Empty is the service's own directory."
        >
          <input
            id={`${ids}-context`}
            className="is-mono"
            value={stage.workingDirectory || ""}
            placeholder={service.workingDirectory || "."}
            disabled={!editable}
            spellCheck={false}
            onChange={(event) => onChange({ workingDirectory: event.target.value })}
          />
        </Field>
        <Field
          label="Image name"
          htmlFor={`${ids}-imagename`}
          optional
          hint="The repository in the registry."
        >
          <input
            id={`${ids}-imagename`}
            className="is-mono"
            value={env[IMAGE_ENV_KEYS.name] || ""}
            placeholder={service.slug}
            disabled={!editable}
            spellCheck={false}
            onChange={(event) => setEnv(IMAGE_ENV_KEYS.name, event.target.value)}
          />
        </Field>
        <Field
          label="Tag"
          htmlFor={`${ids}-imagetag`}
          optional
          hint={
            <>
              Empty tags a branch build <code>&lt;branch&gt;-&lt;build no.&gt;</code> and a tag build
              with the git tag. <code>{"${VAR}"}</code> expands at build time. A build input named{" "}
              <code>IMAGE_TAG</code> wins over this.
            </>
          }
        >
          <input
            id={`${ids}-imagetag`}
            className="is-mono"
            value={env[IMAGE_ENV_KEYS.tag] || ""}
            placeholder="main-42"
            disabled={!editable}
            spellCheck={false}
            onChange={(event) => setEnv(IMAGE_ENV_KEYS.tag, event.target.value)}
          />
        </Field>
      </div>

      <div className={`pl-scan${armed ? " is-on" : ""}`}>
        <div className="pl-scan-head">
          <span className="pl-scan-icon" aria-hidden="true">
            <PlIcon name="shield" />
          </span>
          <div>
            <strong>Scan the image before it is pushed</strong>
            <p>
              BuildKit stops short of the registry; Trivy scans what it built and the push happens
              only on a pass — there is never a vulnerable tag to clean up afterwards.
            </p>
          </div>
          <Switch
            checked={armed}
            disabled={!editable}
            label={armed ? "On" : "Off"}
            onChange={(next) => setScan({ enabled: next })}
          />
        </div>
        {armed && (
          <div className="pl-scan-body">
            <Field
              label="Stop the push on"
              hint="Every severity is recorded in the report either way. High is routinely non-empty on a stock base image."
            >
              <Segmented
                label="Severity that blocks"
                value={scan.threshold || "critical"}
                options={IMAGE_SCAN_THRESHOLDS.map((item) => ({ value: item.value, label: item.label }))}
                disabled={!editable}
                onChange={(threshold) => setScan({ threshold })}
              />
            </Field>
            <Field
              label="When something is found"
              hint={
                (scan.onFail || "block") === "block"
                  ? "The stage fails and the image is not pushed."
                  : "The report is attached and the image is pushed anyway — a gate that only reports."
              }
            >
              <Segmented
                label="When something is found"
                value={scan.onFail || "block"}
                options={IMAGE_SCAN_ON_FAIL.map((item) => ({ value: item.value, label: item.label }))}
                disabled={!editable}
                onChange={(onFail) => setScan({ onFail })}
              />
            </Field>
            <label className="pl-check">
              <input
                type="checkbox"
                checked={Boolean(scan.ignoreUnfixed)}
                disabled={!editable}
                onChange={(event) => setScan({ ignoreUnfixed: event.target.checked })}
              />
              <span>
                <strong>Ignore findings with no fix available</strong>
                <small>
                  A CVE with no released fix cannot be cleared by rebuilding, so counting it blocks a
                  build nobody can unblock. Off by default — that is a policy choice, not
                  KubeSight's.
                </small>
              </span>
            </label>
            <p className="pl-field-hint">
              Needs the KubeSight CI image tools (buildctl + Trivy + crane) mirrored to the cluster
              from <code>Dockerfile.ci-imagetools</code>. Without them the stage fails to start and
              says so — it never falls back to pushing unscanned. Applies from the next build.
            </p>
          </div>
        )}
      </div>

      <p className="pl-field-hint">{CONDITIONAL_STAGE_TYPES.container_image}</p>
    </>
  );
}

function ConditionBuilder({ stage, parameters, editable, onChange, onOpenInputs }) {
  const condition = stage.runCondition || { variable: "", operator: "equals", value: "" };
  const conditional = Boolean(condition.variable);
  const declared = (parameters || []).filter((param) => param.name);
  // A condition saved against an input that was later renamed must still be
  // selectable, or opening the editor would silently drop it on save.
  const orphan = conditional && !declared.some((param) => param.name === condition.variable);
  const param = declared.find((item) => item.name === condition.variable);

  const setCondition = (patch) => onChange({ runCondition: { ...condition, ...patch } });

  if (!declared.length && !conditional) {
    return (
      <div className="pl-empty-inline">
        <p>
          A stage can be made to run only for some builds — only for <code>DEPLOY_ENV = prod</code>,
          only when a <em>Deploy?</em> box is ticked. That needs a build input to ask the question
          first.
        </p>
        {editable && (
          <button type="button" className="btn-outline btn-compact" onClick={onOpenInputs}>
            <PlIcon name="inputs" /> Add a build input
          </button>
        )}
      </div>
    );
  }

  const valueControl = () => {
    if (param?.type === "boolean") {
      return (
        <Segmented
          label="Value"
          value={condition.value === "false" ? "false" : "true"}
          options={[
            { value: "true", label: "Yes" },
            { value: "false", label: "No" },
          ]}
          disabled={!editable}
          onChange={(value) => setCondition({ value })}
        />
      );
    }
    if (param?.type === "choice" && (param.choices || []).filter(Boolean).length) {
      const choices = param.choices.filter(Boolean);
      return (
        <select
          aria-label="Value"
          value={condition.value ?? ""}
          disabled={!editable}
          onChange={(event) => setCondition({ value: event.target.value })}
        >
          {!choices.includes(condition.value) && <option value={condition.value ?? ""}>{condition.value || "Pick one"}</option>}
          {choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
      );
    }
    return (
      <input
        aria-label="Value"
        className="is-mono"
        value={condition.value ?? ""}
        placeholder="prod"
        disabled={!editable}
        onChange={(event) => setCondition({ value: event.target.value })}
      />
    );
  };

  return (
    <div className="pl-condition">
      <Segmented
        label="When this stage runs"
        value={conditional ? "when" : "always"}
        options={[
          { value: "always", label: "Every build" },
          { value: "when", label: "Only when…" },
        ]}
        disabled={!editable}
        onChange={(mode) =>
          onChange({
            runCondition:
              mode === "always"
                ? null
                : {
                    variable: declared[0]?.name || "",
                    operator: "equals",
                    value: declared[0]?.type === "boolean" ? "true" : declared[0]?.choices?.[0] || "",
                  },
          })
        }
      />
      {conditional && (
        <div className="pl-sentence">
          <span>Run only when</span>
          <select
            aria-label="Build input"
            value={condition.variable}
            disabled={!editable}
            onChange={(event) => {
              const next = declared.find((item) => item.name === event.target.value);
              setCondition({
                variable: event.target.value,
                value: next?.type === "boolean" ? "true" : next?.choices?.[0] || condition.value,
              });
            }}
          >
            {declared.map((item) => (
              <option key={item.name} value={item.name}>
                {item.label && item.label !== item.name ? `${item.label} (${item.name})` : item.name}
              </option>
            ))}
            {orphan && <option value={condition.variable}>{condition.variable} (not an input)</option>}
          </select>
          <select
            aria-label="Comparison"
            value={condition.operator || "equals"}
            disabled={!editable}
            onChange={(event) => setCondition({ operator: event.target.value })}
          >
            <option value="equals">is</option>
            <option value="not_equals">is not</option>
          </select>
          {valueControl()}
        </div>
      )}
      {conditional && (
        <p className="pl-field-hint">
          Compared as text against what the build was started with. A skipped stage shows as
          skipped in the build, never as passed.
        </p>
      )}
    </div>
  );
}

function FailureSettings({ ids, stage, editable, onChange, error }) {
  const seconds = Number(stage.timeoutSeconds) || DEFAULT_TIMEOUT_SECONDS;
  return (
    <div className="pl-failure">
      <Field label="If this stage fails">
        <Segmented
          label="If this stage fails"
          value={stage.continueOnFailure ? "continue" : "stop"}
          options={[
            { value: "stop", label: "Stop the build", hint: "Later stages do not run" },
            { value: "continue", label: "Keep going", hint: "Later stages run; the build still fails" },
          ]}
          disabled={!editable}
          onChange={(mode) => onChange({ continueOnFailure: mode === "continue" })}
        />
      </Field>
      <Field
        label="Time limit"
        htmlFor={`${ids}-timeout`}
        error={error}
        hint={`The stage is stopped and marked timed out after ${timeoutLabel(seconds)}. Between 30 seconds and 24 hours.`}
      >
        <div className="pl-timeout">
          <div className="pl-timeout-input">
            <input
              id={`${ids}-timeout`}
              type="number"
              min={0.5}
              max={1440}
              step={0.5}
              value={stage.timeoutSeconds === "" ? "" : Math.round((seconds / 60) * 100) / 100}
              disabled={!editable}
              onChange={(event) =>
                onChange({
                  timeoutSeconds:
                    event.target.value === "" ? "" : Math.round(Number(event.target.value) * 60),
                })
              }
            />
            <span>minutes</span>
          </div>
          {editable && (
            <div className="pl-presets" role="group" aria-label="Common time limits">
              {TIMEOUT_PRESETS.map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  className={`btn-ghost${seconds === value ? " is-on" : ""}`}
                  aria-pressed={seconds === value}
                  onClick={() => onChange({ timeoutSeconds: value })}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
      </Field>
    </div>
  );
}

function SecretPicker({ stage, secretKeys, editable, onChange, onGoToTab }) {
  const refs = stage.secretRefs || [];
  // A reference to a secret that no longer exists is shown so it can be
  // removed — the backend refuses to save it, and hiding it would make that
  // refusal unexplainable.
  const missing = refs.filter((ref) => !secretKeys.some((item) => item.key === ref.name));
  if (!secretKeys.length && !missing.length) {
    return (
      <div className="pl-empty-inline">
        <p>
          No secrets are available to this service yet. Add one — for this service or for every
          service — in Settings, then attach it here.
        </p>
        <button type="button" className="btn-outline btn-compact" onClick={() => onGoToTab?.("settings")}>
          <PlIcon name="lock" /> Manage secrets
        </button>
      </div>
    );
  }
  const toggle = (key, on) =>
    onChange({
      secretRefs: on ? [...refs, { name: key, envVar: key }] : refs.filter((item) => item.name !== key),
    });
  return (
    <div className="pl-secrets">
      {[...secretKeys, ...missing.map((ref) => ({ key: ref.name, scope: "missing" }))].map(({ key, scope }) => {
        const ref = refs.find((item) => item.name === key);
        return (
          <div key={key} className={`pl-secret${ref ? " is-on" : ""}${scope === "missing" ? " is-missing" : ""}`}>
            <label className="pl-secret-pick">
              <input
                type="checkbox"
                checked={Boolean(ref)}
                disabled={!editable}
                onChange={(event) => toggle(key, event.target.checked)}
              />
              <PlIcon name="key" />
              <code>{key}</code>
              {scope === "global" && <span className="pl-tag">global</span>}
              {scope === "missing" && <span className="pl-tag is-error">deleted — remove it</span>}
            </label>
            {ref && (
              <label className="pl-secret-env">
                <span>as</span>
                <input
                  className="is-mono"
                  value={ref.envVar}
                  aria-label={`Environment variable for ${key}`}
                  disabled={!editable}
                  spellCheck={false}
                  onChange={(event) =>
                    onChange({
                      secretRefs: refs.map((item) =>
                        item.name === key ? { ...item, envVar: event.target.value } : item
                      ),
                    })
                  }
                />
              </label>
            )}
          </div>
        );
      })}
      <p className="pl-field-hint">
        Injected as the variable named on the right, masked in the log.{" "}
        <button type="button" className="btn-ghost pl-link" onClick={() => onGoToTab?.("settings")}>
          Manage secrets
        </button>
      </p>
    </div>
  );
}

