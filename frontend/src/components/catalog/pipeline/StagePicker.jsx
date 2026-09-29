import { PlIcon } from "./icons.jsx";
import { STAGE_KINDS } from "./stageModel.js";

/**
 * Choosing what the new stage is, before it exists.
 *
 * Kind first because it decides every field that follows — asking for a name
 * and then changing the kind throws away what was typed. The insertion point
 * is named in words and held open in the flow, so nobody wonders where it went.
 */
export default function StagePicker({ stages, insertAt, onPick, onCopy, onCancel }) {
  const before = stages[insertAt];
  const after = stages[insertAt - 1];
  const where = !stages.length
    ? "as the first stage"
    : insertAt >= stages.length
      ? `at the end, after “${after?.name || `stage ${insertAt}`}”`
      : insertAt === 0
        ? `at the start, before “${before?.name || "stage 1"}”`
        : `between “${after?.name || `stage ${insertAt}`}” and “${before?.name || `stage ${insertAt + 1}`}”`;

  const hasCheckout = stages.some((stage) => stage.stageType === "checkout");

  return (
    <section className="pl-picker" aria-labelledby="pl-picker-title">
      <header className="pl-picker-head">
        <span className="pl-kicker">New stage · {where}</span>
        <h3 id="pl-picker-title">What should this stage do?</h3>
        <p>Pick a kind — it decides which settings the stage has. You can rename and reorder it after.</p>
      </header>

      <div className="pl-picker-grid">
        {STAGE_KINDS.map((kind) => (
          <button
            key={kind.value}
            type="button"
            className={`btn-ghost pl-picker-card kind-${kind.value}`}
            onClick={() => onPick(kind.value)}
          >
            <span className="pl-picker-glyph" aria-hidden="true">
              <PlIcon name={kind.icon} />
            </span>
            <strong>{kind.verb}</strong>
            <small>{kind.description}</small>
            {kind.value === "checkout" && hasCheckout && (
              <span className="pl-tag">This pipeline already has one</span>
            )}
            <span className="pl-picker-cta" aria-hidden="true">
              Add <PlIcon name="chevron" />
            </span>
          </button>
        ))}
      </div>

      {stages.length > 0 && (
        <div className="pl-picker-copy">
          <label htmlFor="pl-picker-copy-select">
            <PlIcon name="copy" /> Or start from a copy of
          </label>
          <select
            id="pl-picker-copy-select"
            value=""
            onChange={(event) => event.target.value !== "" && onCopy(Number(event.target.value))}
          >
            <option value="">Choose a stage…</option>
            {stages.map((stage, index) => (
              <option key={index} value={index}>
                {index + 1}. {stage.name || "Unnamed stage"}
              </option>
            ))}
          </select>
        </div>
      )}

      {onCancel && (
        <div className="pl-picker-foot">
          <button type="button" className="btn-outline btn-compact" onClick={onCancel}>
            Cancel
          </button>
        </div>
      )}
    </section>
  );
}
