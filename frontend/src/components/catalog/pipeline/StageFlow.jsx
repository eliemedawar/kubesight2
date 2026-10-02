import { useEffect, useRef, useState } from "react";
import { PlIcon } from "./icons.jsx";
import { kindOf, shortImage, stageFlags, stageSummary } from "./stageModel.js";
import { groupFailFast, parallelGroups } from "./parallelModel.js";

/** Where the stage being added will land, held open in the flow while the
 * kind is chosen — "added at the end" is otherwise only a sentence. */
function NewStageGhost() {
  return (
    <span className="pl-flow-ghost" aria-hidden="true">
      <PlIcon name="plus" />
      New stage goes here
    </span>
  );
}

/**
 * The pipeline as it runs: a vertical flow from "build starts" to "done".
 *
 * Every stage says what it is (its kind's glyph), what it does (one line),
 * and what changes its behaviour (marks) without being opened — so an
 * eleven-stage Jenkinsfile import reads as a pipeline, not a list of names.
 *
 * Reordering works three ways: drag the grip, Alt+Arrow on a focused stage,
 * or the move buttons on the stage sheet. All three are one local array move;
 * the pipeline saves in one request.
 */
export default function StageFlow({
  stages,
  selectedIndex,
  mode,
  insertAt,
  problems,
  changes,
  editable,
  branch,
  onSelect,
  onMove,
  onInsert,
  // Rendered after "Build finishes": the post actions block, which happens then.
  endSlot = null,
}) {
  const [dragFrom, setDragFrom] = useState(null);
  const [dropAt, setDropAt] = useState(null);
  const itemRefs = useRef([]);
  const flowRef = useRef(null);

  // Keep the selected stage in sight inside the flow's own scroll box — a
  // deep link to stage 9, or a pick from the health strip, otherwise lands on
  // a sheet whose stage is scrolled out of the flow. Only the flow scrolls;
  // scrollIntoView would drag the whole page along with it.
  useEffect(() => {
    const box = flowRef.current;
    const card = itemRefs.current[selectedIndex];
    if (!box || !card || box.scrollHeight <= box.clientHeight) return;
    // Rects, not offsetTop: each card's offsetParent is its own <li>.
    const top = card.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
    const bottom = top + card.offsetHeight;
    if (top < box.scrollTop + 40) box.scrollTop = Math.max(0, top - 40);
    else if (bottom > box.scrollTop + box.clientHeight - 40) {
      box.scrollTop = bottom - box.clientHeight + 40;
    }
  }, [selectedIndex, stages.length]);

  const focusItem = (index) => {
    window.requestAnimationFrame(() => itemRefs.current[index]?.focus());
  };

  const onItemKey = (event, index) => {
    const delta = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
    if (!delta) return;
    event.preventDefault();
    const target = index + delta;
    if (target < 0 || target >= stages.length) return;
    if (event.altKey && editable) {
      onMove(index, target);
    } else {
      onSelect(target);
    }
    focusItem(target);
  };

  const finishDrag = () => {
    if (dragFrom !== null && dropAt !== null) {
      // dropAt is a gap (0..n); moving down past yourself shifts the gap by one.
      const target = dropAt > dragFrom ? dropAt - 1 : dropAt;
      if (target !== dragFrom) onMove(dragFrom, target);
    }
    setDragFrom(null);
    setDropAt(null);
  };

  return (
    <nav className="pl-flow" ref={flowRef} aria-label="Pipeline stages in run order">
      <div className="pl-flow-head">
        <span>Run order</span>
        {editable && stages.length > 1 && (
          <small title="Or focus a stage and press Alt+↑ / Alt+↓">Drag to reorder</small>
        )}
      </div>

      <div className="pl-flow-terminus is-start">
        <span className="pl-flow-terminus-dot" aria-hidden="true">
          <PlIcon name="sparkle" />
        </span>
        <span>
          <strong>Build starts</strong>
          <small>
            {branch ? (
              <>
                on the ref picked at run time · default <code>{branch}</code>
              </>
            ) : (
              "on the branch or tag picked at run time"
            )}
          </small>
        </span>
      </div>

      <ol
        className={`pl-flow-list${dragFrom !== null ? " is-dragging" : ""}`}
        onDragOver={(event) => dragFrom !== null && event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          finishDrag();
        }}
      >
        {flowSteps(stages).map((step) =>
          step.run ? (
            <li
              key={`group-${step.run.start}-${step.run.key}`}
              className={`pl-flow-group${
                step.run.indices.includes(selectedIndex) && mode === "stage" ? " has-selected" : ""
              }`}
            >
              <div className="pl-flow-group-head">
                <span className="pl-flow-group-glyph" aria-hidden="true">
                  <PlIcon name="parallel" />
                </span>
                <span className="pl-flow-group-title">
                  <strong>{step.run.name || "Parallel group"}</strong>
                  <small>
                    {step.run.indices.length} at once
                    {groupFailFast(stages, step.run) ? " · stops at the first failure" : ""}
                  </small>
                </span>
              </div>
              <ol className="pl-flow-lanes" aria-label={`Run at the same time: ${step.run.name || "parallel group"}`}>
                {step.run.indices.map((index) => renderItem(stages[index], index, true))}
              </ol>
            </li>
          ) : (
            renderItem(stages[step.index], step.index, false)
          )
        )}
        {dropAt === stages.length && dragFrom !== null && (
          <li className="pl-flow-drop-end" aria-hidden="true">
            <span className="pl-flow-drop" />
          </li>
        )}
        {mode === "add" && insertAt >= stages.length && (
          <li className="pl-flow-item is-ghost-end">
            <NewStageGhost />
          </li>
        )}
      </ol>

      {editable && (
        <button
          type="button"
          className={`btn-ghost pl-flow-add${mode === "add" && insertAt === stages.length ? " is-on" : ""}`}
          onClick={() => onInsert(stages.length)}
        >
          <span className="pl-flow-add-icon" aria-hidden="true">
            <PlIcon name="plus" />
          </span>
          <span>
            <strong>Add a stage</strong>
            <small>At the end of the pipeline</small>
          </span>
        </button>
      )}

      <div className="pl-flow-terminus is-end">
        <span className="pl-flow-terminus-dot" aria-hidden="true">
          <PlIcon name="check" />
        </span>
        <span>
          <strong>Build finishes</strong>
          <small>Artifacts and images land on the build</small>
        </span>
      </div>
      {endSlot}
    </nav>
  );

  // One stage's row. Inside a group it is a lane off the group's bracket.
  function renderItem(stage, index, inGroup) {
    const kind = kindOf(stage.stageType);
    const selected = mode === "stage" && selectedIndex === index;
    const stageProblems = problems[index] || [];
    const errors = stageProblems.filter((item) => item.level !== "warning").length;
    const warnings = stageProblems.length - errors;
    const change = changes[index];
    const flags = stageFlags(stage);
    const summary = stageSummary(stage);
    const mono = stage.stageType === "command" && summary !== "No commands yet";
    return (
      <li
        key={stage._key ?? stage.id ?? `new-${index}`}
        className={`pl-flow-item kind-${kind?.value || "retired"}${
          stage.enabled === false ? " is-off" : ""
        }${dragFrom === index ? " is-dragged" : ""}${inGroup ? " in-lane" : ""}`}
        draggable={editable}
        onDragStart={(event) => {
          setDragFrom(index);
          event.dataTransfer.effectAllowed = "move";
          // Firefox refuses to start a drag without data.
          event.dataTransfer.setData("text/plain", String(index));
        }}
        onDragOver={(event) => {
          if (dragFrom === null) return;
          event.preventDefault();
          const box = event.currentTarget.getBoundingClientRect();
          setDropAt(event.clientY < box.top + box.height / 2 ? index : index + 1);
        }}
        onDragEnd={() => {
          setDragFrom(null);
          setDropAt(null);
        }}
      >
        {editable && (
          <button
            type="button"
            className={`btn-ghost pl-flow-insert${insertAt === index && mode === "add" ? " is-on" : ""}`}
            onClick={() => onInsert(index)}
            aria-label={`Insert a stage before ${stage.name || `stage ${index + 1}`}`}
            title="Insert a stage here"
          >
            <PlIcon name="plus" />
          </button>
        )}
        {dropAt === index && dragFrom !== null && <span className="pl-flow-drop" aria-hidden="true" />}
        {mode === "add" && insertAt === index && <NewStageGhost />}
        <button
          type="button"
          ref={(node) => {
            itemRefs.current[index] = node;
          }}
          className={`btn-ghost pl-flow-card${selected ? " is-selected" : ""}${
            errors ? " has-errors" : warnings ? " has-warnings" : ""
          }`}
          aria-current={selected ? "step" : undefined}
          aria-describedby={`pl-flow-meta-${index}`}
          onClick={() => onSelect(index)}
          onKeyDown={(event) => onItemKey(event, index)}
        >
          {editable && (
            <span className="pl-flow-grip" aria-hidden="true">
              <PlIcon name="grip" />
            </span>
          )}
          <span className="pl-flow-node" aria-hidden="true">
            <PlIcon name={kind?.icon || "alert"} />
            <span className="pl-flow-number">{index + 1}</span>
          </span>
          <span className="pl-flow-copy">
            <strong>{stage.name || <em>Unnamed stage</em>}</strong>
            <small className={mono ? "is-mono" : ""}>
              {stage.stageType === "command" && stage.image && (
                <span className="pl-flow-image">{shortImage(stage.image)}</span>
              )}
              {summary}
            </small>
            {(flags.length > 0 || change) && (
              <span className="pl-flow-marks" id={`pl-flow-meta-${index}`}>
                {change && (
                  <span className={`pl-flow-change is-${change}`}>
                    {change === "new" ? "New" : "Edited"}
                  </span>
                )}
                {flags.map((flag) => (
                  <span key={flag.key} className={`pl-flow-flag is-${flag.key}`} title={flag.label}>
                    <PlIcon name={flag.icon} />
                    <span className="pl-sr">{flag.label}</span>
                    {flag.key === "when" && (
                      <span className="pl-flow-flag-text">{stage.runCondition.variable}</span>
                    )}
                    {flag.key === "off" && <span className="pl-flow-flag-text">Off</span>}
                  </span>
                ))}
              </span>
            )}
          </span>
          {stageProblems.length > 0 && (
            <span
              className={`pl-flow-problem${errors ? " is-error" : ""}`}
              title={stageProblems.map((item) => item.message).join("\n")}
            >
              <PlIcon name="alert" />
              <span>{stageProblems.length}</span>
              <span className="pl-sr"> {stageProblems.length === 1 ? "problem" : "problems"}</span>
            </span>
          )}
        </button>
      </li>
    );
  }
}

/** The flow's rows: a lone stage, or a parallel group of two or more. */
function flowSteps(stages) {
  const runs = parallelGroups(stages);
  const steps = [];
  for (let index = 0; index < stages.length; index += 1) {
    const run = runs.find((item) => item.start === index);
    if (run) {
      steps.push({ run });
      index = run.end;
    } else {
      steps.push({ index });
    }
  }
  return steps;
}
