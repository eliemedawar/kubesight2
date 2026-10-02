import { PlIcon } from "./icons.jsx";
import {
  MAX_POST_ACTIONS,
  postActionSummary,
  postActionTitle,
  postActionType,
  postActionsSentence,
  whenLabel,
} from "./postActionModel.js";

/**
 * "When the build ends" — the pipeline's post actions, hung off the end of
 * the flow, after "Build finishes", because that is when they happen.
 *
 * Each row says what it is (glyph), when it fires (chip), and what it does
 * (one line), like a stage card; selecting one opens it in the sheet.
 */
export default function PostActionsBlock({
  actions,
  selectedIndex,
  adding,
  problems,
  changes,
  editable,
  onSelect,
  onAdd,
}) {
  const full = actions.length >= MAX_POST_ACTIONS;
  return (
    <section className="pl-post" aria-labelledby="pl-post-title">
      <header className="pl-post-head">
        <span className="pl-post-head-dot" aria-hidden="true">
          <PlIcon name="forward" />
        </span>
        <span>
          <strong id="pl-post-title">When the build ends</strong>
          <small>{postActionsSentence(actions)}</small>
        </span>
      </header>

      {actions.length > 0 && (
        <ol className="pl-post-list">
          {actions.map((action, index) => {
            const kind = postActionType(action.type);
            const issues = problems[index] || [];
            const selected = selectedIndex === index && !adding;
            const change = changes[index];
            return (
              <li key={action._key ?? index} className={`pl-post-item is-${action.type}`}>
                <button
                  type="button"
                  className={`btn-ghost pl-post-card${selected ? " is-selected" : ""}${
                    issues.length ? " has-errors" : ""
                  }`}
                  aria-current={selected ? "true" : undefined}
                  onClick={() => onSelect(index)}
                >
                  <span className="pl-post-glyph" aria-hidden="true">
                    <PlIcon name={kind?.icon || "alert"} />
                  </span>
                  <span className="pl-post-copy">
                    <strong>
                      {postActionTitle(action)}
                      <span className={`pl-post-when is-${action.when || "always"}`}>{whenLabel(action.when)}</span>
                    </strong>
                    <small className={action.type === "commands" ? "is-mono" : ""}>{postActionSummary(action)}</small>
                    {change && (
                      <span className={`pl-flow-change is-${change}`}>{change === "new" ? "New" : "Edited"}</span>
                    )}
                  </span>
                  {issues.length > 0 && (
                    <span className="pl-flow-problem is-error" title={issues.map((item) => item.message).join("\n")}>
                      <PlIcon name="alert" />
                      <span>{issues.length}</span>
                      <span className="pl-sr"> {issues.length === 1 ? "problem" : "problems"}</span>
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ol>
      )}

      {editable && (
        <button
          type="button"
          className={`btn-ghost pl-post-add${adding ? " is-on" : ""}`}
          onClick={onAdd}
          disabled={full}
          title={full ? `A pipeline can have at most ${MAX_POST_ACTIONS} post actions.` : undefined}
        >
          <PlIcon name="plus" />
          <span>
            <strong>Add a post action</strong>
            <small>{full ? `At most ${MAX_POST_ACTIONS}` : "Email, webhook or cleanup"}</small>
          </span>
        </button>
      )}
      {!editable && actions.length === 0 && (
        <p className="pl-post-empty">No notifications or cleanup after a build.</p>
      )}
    </section>
  );
}
