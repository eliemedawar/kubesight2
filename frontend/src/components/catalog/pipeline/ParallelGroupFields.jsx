import { Field, Switch } from "./controls.jsx";
import { PlIcon } from "./icons.jsx";
import {
  canJoinPrevious,
  groupAt,
  groupFailFast,
  groupMembers,
  joinedToPrevious,
  MAX_GROUP_NAME,
  MAX_GROUP_SIZE,
} from "./parallelModel.js";

/** What the closed "Run in parallel" row says. */
export function parallelSummary(stages, index) {
  const run = groupAt(stages, index);
  if (!run || run.indices.length < 2) return "No — after the stage before";
  const others = run.indices.filter((position) => position !== index).map((position) => stages[position]?.name || "Unnamed");
  return others.length === 1 ? `With ${others[0]}` : `With ${others[0]} + ${others.length - 1}`;
}

/**
 * The "Run in parallel" settings of one stage.
 *
 * One switch — run alongside the stage before — because that is how people
 * think about it ("lint can run while the tests run"), and the group itself
 * follows: its members, its name, and its one fail-fast switch. The shared
 * workspace is said plainly, because nothing can check it for them.
 */
export default function ParallelGroupFields({ ids, stages, index, editable, onGroupChange, capability, error }) {
  const stage = stages[index];
  const run = groupAt(stages, index);
  const grouped = Boolean(run && run.indices.length >= 2);
  const joined = joinedToPrevious(stages, index);
  const join = canJoinPrevious(stages, index);
  const members = grouped ? groupMembers(stages, index) : [];
  const failFast = grouped && groupFailFast(stages, run);
  const kubernetes = capability?.kubernetes;
  const sequentialReason =
    capability?.mode === "off"
      ? "parallel stages are switched off on this installation (CI_PARALLEL_STAGES=off)."
      : kubernetes && !kubernetes.supported
        ? kubernetes.reason
        : "";

  return (
    <div className="pl-parallel">
      <div className="pl-parallel-switch">
        <Switch
          checked={joined}
          disabled={!editable || (!joined && !join.ok)}
          label="Run in parallel with the stage before"
          describedBy={`${ids}-parallel-hint`}
          onChange={(next) => onGroupChange(next ? "join" : "leave")}
        />
        <p id={`${ids}-parallel-hint`} className="pl-field-hint">
          {!joined && !join.ok
            ? join.reason
            : joined
              ? `Starts at the same time as “${stages[index - 1]?.name || "the stage before"}”.`
              : "Starts together with the stage before it; the pipeline continues once both are done."}
        </p>
      </div>

      {grouped && (
        <div className="pl-parallel-group" role="group" aria-label={`Parallel group ${run.name}`}>
          <div className="pl-parallel-head">
            <span className="pl-parallel-glyph" aria-hidden="true">
              <PlIcon name="parallel" />
            </span>
            <div>
              <strong>
                {members.length} stages run together
              </strong>
              <small>The next stage starts once all of them are done.</small>
            </div>
          </div>

          <ol className="pl-parallel-members" aria-label="Stages in this group">
            {members.map(({ index: position, stage: member }) => (
              <li
                key={member._key ?? member.id ?? position}
                className={`${position === index ? "is-current" : ""}${member.enabled === false ? " is-off" : ""}`}
              >
                <span className="pl-parallel-number">{position + 1}</span>
                <span className="pl-parallel-name">{member.name || "Unnamed stage"}</span>
                {member.enabled === false && <span className="pl-parallel-tag">Off</span>}
                {member.continueOnFailure && <span className="pl-parallel-tag">Keeps going</span>}
              </li>
            ))}
          </ol>

          <div className="pl-grid">
            <Field
              label="Group name"
              htmlFor={`${ids}-group-name`}
              error={error}
              hint={`Shown on the bracket in the flow and on builds. Up to ${MAX_GROUP_SIZE} stages per group.`}
            >
              <input
                id={`${ids}-group-name`}
                value={stage.parallelGroup || ""}
                maxLength={MAX_GROUP_NAME + 10}
                disabled={!editable}
                onChange={(event) => onGroupChange("rename", event.target.value)}
              />
            </Field>
            <Field label="When one of them fails">
              <Switch
                checked={failFast}
                disabled={!editable}
                label={failFast ? "Stop the others" : "Let the others finish"}
                onChange={(next) => onGroupChange("failFast", next)}
              />
              <p className="pl-field-hint">
                {failFast
                  ? "The first failure stops the stages still running; the group fails."
                  : "Like Jenkins parallel: the others finish, then the group fails and later stages are skipped."}
              </p>
            </Field>
          </div>

          <div className="pl-note is-info">
            <PlIcon name="alert" />
            <p>
              <strong>One workspace, shared.</strong> These stages run at the same moment on the same
              checkout, so they must not write the same files or depend on each other. A variable one
              of them writes to <code>$KUBESIGHT_ENV</code> reaches the stages after the group, not
              its siblings.
            </p>
          </div>

          {sequentialReason && (
            <div className="pl-note is-warn">
              <PlIcon name="clock" />
              <p>
                <strong>Runs one stage at a time on this installation’s Kubernetes runner:</strong>{" "}
                {sequentialReason} The group keeps its rules — it only takes longer.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
