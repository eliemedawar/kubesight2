import { StageStatusIcon } from "./ciShared.jsx";
import { buildSteps, stepStatus } from "./pipeline/parallelModel.js";

/**
 * Checkout → Build → Test → Scan → Image → Publish.
 *
 * Doubles as the pipeline shape (no statuses passed) and as a build's live
 * progress (statuses passed). One component so the two never drift apart
 * visually.
 *
 * A parallel group is one step of the strip: its stages stacked in one column
 * under a bracket, each with its own live status, because they run at the same
 * time rather than one after another.
 */
export default function PipelineStrip({ stages = [], activeStageId, onSelectStage }) {
  if (!stages.length) return null;
  const steps = buildSteps(stages);

  const node = (stage, index) => {
    const status = stage.status || "definition";
    const clickable = Boolean(onSelectStage);
    const Tag = clickable ? "button" : "div";
    return (
      <Tag
        key={stage.id ?? `${stage.name}-${index}`}
        type={clickable ? "button" : undefined}
        className={`sg-ci-strip-node sg-ci-strip-node--${status}${
          activeStageId === stage.id ? " is-active" : ""
        }`}
        onClick={clickable ? () => onSelectStage(stage) : undefined}
        aria-current={activeStageId === stage.id ? "step" : undefined}
        title={stage.error || stage.name}
      >
        {/* A status glyph only when there IS a status — a definition
            strip showing "pending" clocks reads as a stuck build. */}
        {stage.status && (
          <span className="sg-ci-strip-icon">
            <StageStatusIcon status={stage.status} />
          </span>
        )}
        <span className="sg-ci-strip-name">{stage.name}</span>
      </Tag>
    );
  };

  return (
    <ol className="sg-ci-strip" aria-label="Pipeline stages">
      {steps.map((step, index) => (
        <li
          key={step.group ? `group-${step.items[0].id ?? step.items[0].name}` : step.items[0].id ?? `${step.items[0].name}-${index}`}
          className={`sg-ci-strip-item${step.group ? " is-group" : ""}`}
        >
          {step.group ? (
            <div
              className={`sg-ci-strip-group sg-ci-strip-group--${
                step.items.some((item) => item.status) ? stepStatus(step.items) : "definition"
              }`}
              role="group"
              aria-label={`${step.group}: ${step.items.length} stages at the same time`}
            >
              <span className="sg-ci-strip-group-label" title={step.failFast ? "Stops at the first failure" : undefined}>
                {step.group}
              </span>
              <div className="sg-ci-strip-lanes">{step.items.map((stage, lane) => node(stage, `${index}-${lane}`))}</div>
            </div>
          ) : (
            node(step.items[0], index)
          )}
          {index < steps.length - 1 && <span className="sg-ci-strip-arrow" aria-hidden="true" />}
        </li>
      ))}
    </ol>
  );
}
