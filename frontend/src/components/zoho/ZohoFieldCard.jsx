import { VALUE_CHIP_CAP, fieldActions, fieldHint } from "./zohoFieldMeta";
import { useTicketing } from "../ticketing/TicketingContext.jsx";
import ZohoActionMenu from "./ZohoActionMenu.jsx";

/**
 * One field on the form mirror. Purely presentational — every button just
 * dispatches its action key back to the editor, which owns the modals.
 */
export default function ZohoFieldCard({
  field,
  role,
  canManage,
  sectionName,
  sectionNames,
  onAction,
}) {
  const { capabilities } = useTicketing();
  const values = (field.allowedValues || []).filter((v) => v !== "-None-");
  const hint = fieldHint(role, field);
  const actions = fieldActions(field, role, canManage, capabilities);
  if (canManage && sectionNames.some((name) => name !== sectionName)) {
    const editIndex = actions.findIndex((action) => action.key === "edit");
    actions.splice(editIndex < 0 ? actions.length : editIndex + 1, 0, {
      key: "move",
      label: "Move to section",
      variant: "btn-ghost",
    });
  }
  const primaryAction = actions.find((action) => action.key === "edit");
  const secondaryActions = actions
    .filter((action) => action.key !== "edit")
    .map((action) => ({ ...action, danger: action.key === "delete" }));
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
          {/* An empty type (the field catalogue had no schema for it) would
              otherwise render as a blank pill. `title` keeps the provider's own
              name for the type — Jira's is a long plugin key the chip shortens. */}
          {field.type ? (
            <span className="sg-tag" title={field.typeKey || field.type}>
              {field.type}
            </span>
          ) : null}
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
            {field.allowedValues == null ? (
              <span className="muted">options load when opened</span>
            ) : values.length === 0 ? (
              <span className="muted">no options</span>
            ) : null}
            {values.length > VALUE_CHIP_CAP ? (
              <span className="sg-zh-more">+{values.length - VALUE_CHIP_CAP} more</span>
            ) : null}
          </div>
          {hint ? <div className="sg-zh-fhint">{hint}</div> : null}
        </>
      ) : null}

      {actions.length ? (
        <footer>
          {primaryAction ? (
            <button
              type="button"
              className="btn-ghost"
              onClick={() => onAction(primaryAction.key, field, sectionName)}
            >
              {primaryAction.label}
            </button>
          ) : null}
          <ZohoActionMenu
            label={`More actions for ${field.label}`}
            items={secondaryActions}
            onAction={(action) => onAction(action, field, sectionName)}
          />
        </footer>
      ) : null}
    </div>
  );
}
