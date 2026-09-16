import { useState } from "react";

/**
 * The proposed pipeline, before anybody agrees to it.
 *
 * Read as a flow rather than a form: the stages in order, what each one runs,
 * and where. The commands are shown in full and never behind a "details"
 * toggle — a person approving a pipeline is approving the commands, and a
 * review screen that hides them is a rubber stamp with extra steps.
 *
 * Validation findings sit next to the stage they are about. KubeSight's
 * objections and Hermes's objections are worded identically because they are
 * the same objections; the user is simply seeing the round the model did not
 * manage to fix.
 */

const stageKindLabel = (stage) => {
  if (stage.stageType === "checkout") return "Checkout";
  if (stage.stageType === "container_image") return "Container image";
  if (stage.stageType === "publish_artifact") return "Publish artifact";
  if (stage.stageType === "scan") return "Security scan";
  return "";
};

const minutes = (seconds) => {
  const value = Number(seconds) || 0;
  if (value < 90) return `${value}s`;
  return `${Math.round(value / 60)} min`;
};

function Findings({ items, tone }) {
  if (!items?.length) return null;
  return (
    <ul className={`sg-ci-gen-findings is-${tone}`}>
      {items.map((item, index) => (
        <li key={`${item.code}-${index}`}>
          {item.stage && <strong>{item.stage}: </strong>}
          {item.message}
        </li>
      ))}
    </ul>
  );
}

export default function GeneratedPipelineReview({
  pipeline,
  validation,
  onEditStages,
  editable = false,
}) {
  const [openStage, setOpenStage] = useState(null);
  if (!pipeline?.stages?.length) return null;

  const errors = validation?.errors || [];
  const warnings = validation?.warnings || [];
  const forStage = (name, items) => items.filter((item) => item.stage === name);
  const general = (items) => items.filter((item) => !item.stage);

  return (
    <section className="sg-ci-gen" aria-label="Generated pipeline">
      <header className="sg-ci-gen-head">
        <div>
          <h4>Generated pipeline</h4>
          <p className="muted">
            {pipeline.stages.length} stages
            {pipeline.parameters?.length
              ? ` · asks ${pipeline.parameters.length} build ${
                  pipeline.parameters.length === 1 ? "input" : "inputs"
                }`
              : ""}
          </p>
        </div>
        {editable && onEditStages && (
          <button type="button" className="btn-outline btn-compact" onClick={onEditStages}>
            Edit pipeline
          </button>
        )}
      </header>

      {/* Three states, and the middle one is the common one: the pipeline is
          Hermes's, KubeSight has notes on it, and the decision is yours. */}
      {errors.length > 0 ? (
        <div className="sg-ci-gen-verdict is-invalid">
          <strong>This pipeline cannot be stored.</strong> Nothing in it survived
          normalization. Fix the stages below, or configure the pipeline by hand.
          <Findings items={general(errors)} tone="error" />
        </div>
      ) : warnings.length > 0 ? (
        <div className="sg-ci-gen-verdict is-warn">
          <strong>
            {warnings.length} thing{warnings.length === 1 ? "" : "s"} to check before you
            save.
          </strong>{" "}
          KubeSight kept the pipeline as Hermes proposed it and noted what it would have
          objected to. Read the notes on each stage — they are things that will bite at
          build time, not reasons you cannot save.
          <Findings items={general(warnings)} tone="warn" />
        </div>
      ) : (
        <div className="sg-ci-gen-verdict is-valid">
          <strong>Nothing to flag.</strong> Every stage runs on a runner this KubeSight
          has, with an approved build image, and references its secrets rather than
          containing them.
        </div>
      )}

      <ol className="sg-ci-gen-stages">
        {pipeline.stages.map((stage, index) => {
          const stageErrors = forStage(stage.name, errors);
          const stageWarnings = forStage(stage.name, warnings);
          const open = openStage === index;
          const kind = stageKindLabel(stage);
          return (
            <li
              key={`${stage.name}-${index}`}
              className={
                stageErrors.length ? "has-error" : stageWarnings.length ? "has-warning" : ""
              }
            >
              <button
                type="button"
                className="sg-ci-gen-stage"
                aria-expanded={open}
                onClick={() => setOpenStage(open ? null : index)}
              >
                <span className="sg-ci-gen-num">{index + 1}</span>
                <span className="sg-ci-gen-name">
                  {stage.name}
                  {kind && <em>{kind}</em>}
                </span>
                <span className="sg-ci-gen-where">
                  {stage.image ? <code>{stage.image}</code> : null}
                  {(stage.runnerLabels || []).map((label) => (
                    <span key={label} className="sg-ci-gen-label">
                      {label}
                    </span>
                  ))}
                  <span className="sg-ci-gen-timeout">{minutes(stage.timeoutSeconds)}</span>
                </span>
              </button>

              {/* A stage with notes opens itself: the note is the reason
                  somebody is looking at this screen at all. */}
              {(open || stageErrors.length > 0 || stageWarnings.length > 0) && (
                <div className="sg-ci-gen-detail">
                  {/* Approving a pipeline is approving these lines. They are
                      never summarised away. */}
                  {stage.commands?.length > 0 && (
                    <pre className="sg-ci-gen-commands">{stage.commands.join("\n")}</pre>
                  )}
                  {stage.stageType === "checkout" && (
                    <p className="muted">
                      KubeSight performs the checkout itself; this stage runs no commands.
                    </p>
                  )}
                  {stage.stageType === "container_image" && (
                    <p className="muted">
                      BuildKit builds the Dockerfile and pushes to the linked registry;
                      this stage runs no commands.
                    </p>
                  )}

                  <dl className="sg-ci-gen-facts">
                    {stage.workingDirectory && (
                      <div>
                        <dt>Working directory</dt>
                        <dd>
                          <code>{stage.workingDirectory}</code>
                        </dd>
                      </div>
                    )}
                    {stage.artifacts?.length > 0 && (
                      <div>
                        <dt>Artifacts</dt>
                        <dd>
                          {stage.artifacts.map((item) => (
                            <code key={item.path}>{item.path}</code>
                          ))}
                        </dd>
                      </div>
                    )}
                    {stage.secretRefs?.length > 0 && (
                      <div>
                        <dt>Secrets</dt>
                        <dd>
                          {/* Names only, here and everywhere else. */}
                          {stage.secretRefs.map((ref) => (
                            <code key={ref.name}>{ref.name}</code>
                          ))}
                        </dd>
                      </div>
                    )}
                    {Object.keys(stage.env || {}).length > 0 && (
                      <div>
                        <dt>Environment</dt>
                        <dd>
                          {Object.entries(stage.env).map(([key, value]) => (
                            <code key={key}>
                              {key}={value}
                            </code>
                          ))}
                        </dd>
                      </div>
                    )}
                    {stage.runCondition?.variable && (
                      <div>
                        <dt>Runs when</dt>
                        <dd>
                          <code>
                            {stage.runCondition.variable}{" "}
                            {stage.runCondition.operator === "not_equals" ? "≠" : "="}{" "}
                            {stage.runCondition.value}
                          </code>
                        </dd>
                      </div>
                    )}
                  </dl>

                  <Findings items={stageErrors} tone="error" />
                  <Findings items={stageWarnings} tone="warn" />
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
