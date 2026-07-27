import { VALUE_CHIP_CAP, fieldActions, fieldHint } from "./zohoFieldMeta";

/**
 * One field on the layout mirror. Purely presentational — every button just
 * dispatches its action key back to the editor, which owns the modals.
 */
export default function ZohoFieldCard({ field, role, canManage, onAction }) {
  const values = (field.allowedValues || []).filter((v) => v !== "-None-");
  const hint = fieldHint(role, field);
  const actions = fieldActions(field, role, canManage);
  const binding = field.binding && !field.binding.locked ? field.binding : null;

  return (
    <div className="sg-zh-field">
      <header>
        <div className="sg-zh-fname">
          <b>
            {field.label}
            {field.required ? (
              <span className="sg-zh-req" title="Required">
                *
              </span>
            ) : null}
          </b>
          <span className="sg-zh-fapi">{field.apiName}</span>
        </div>
        <div className="sg-zh-fbadges">
          <span className="sg-tag">{field.type}</span>
          {field.autoManaged ? (
            <span
              className="status-pill ok"
              title="Published by the KubeSight sync (deployments / namespaces). Manual edits here are overwritten on the next sync."
            >
              auto-synced
            </span>
          ) : null}
          {binding ? (
            <span
              className={`sg-tag sg-tag-bound${binding.enabled ? "" : " sg-tag-off"}`}
              title={
                binding.lastMessage ||
                `Options published from “${binding.sourceLabel}” on every sync.`
              }
            >
              {binding.enabled ? binding.sourceLabel : `${binding.sourceLabel} (paused)`}
            </span>
          ) : null}
          {binding?.lastStatus === "error" ? (
            <span className="status-pill danger" title={binding.lastMessage || ""}>
              source failed
            </span>
          ) : null}
        </div>
      </header>

      {field.isPicklist ? (
        <>
          <div className="sg-zh-tags sg-zh-fvals">
            {values.slice(0, VALUE_CHIP_CAP).map((v) => (
              <span key={v} className="sg-tag">
                {v}
              </span>
            ))}
            {values.length === 0 ? <span className="muted">no options</span> : null}
            {values.length > VALUE_CHIP_CAP ? (
              <span className="sg-zh-more">+{values.length - VALUE_CHIP_CAP} more</span>
            ) : null}
          </div>
          {hint ? <div className="sg-zh-fhint">{hint}</div> : null}
        </>
      ) : null}

      {actions.length ? (
        <footer>
          {actions.map((action) => (
            <button
              key={action.key}
              type="button"
              className={action.variant}
              onClick={() => onAction(action.key, field)}
            >
              {action.label}
            </button>
          ))}
        </footer>
      ) : null}
    </div>
  );
}
