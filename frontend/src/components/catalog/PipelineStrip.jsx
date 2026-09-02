import { StageStatusIcon } from "./ciShared.jsx";

/**
 * Checkout → Build → Test → Scan → Image → Publish.
 *
 * Doubles as the pipeline shape (no statuses passed) and as a build's live
 * progress (statuses passed). One component so the two never drift apart
 * visually.
 */
export default function PipelineStrip({ stages = [], activeStageId, onSelectStage }) {
  if (!stages.length) return null;

  return (
    <ol className="sg-ci-strip" aria-label="Pipeline stages">
      {stages.map((stage, index) => {
        const status = stage.status || "definition";
        const clickable = Boolean(onSelectStage);
        const Tag = clickable ? "button" : "div";
        return (
          <li key={stage.id ?? `${stage.name}-${index}`} className="sg-ci-strip-item">
            <Tag
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
            {index < stages.length - 1 && <span className="sg-ci-strip-arrow" aria-hidden="true" />}
          </li>
        );
      })}
    </ol>
  );
}
