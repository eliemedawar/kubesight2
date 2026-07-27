import ZohoFieldCard from "./ZohoFieldCard.jsx";
import { fieldRole } from "./zohoFieldMeta";
import ZohoActionMenu from "./ZohoActionMenu.jsx";
import { IconChevronDown } from "./icons.jsx";

/** One layout section: its rule-and-label header plus the grid of field cards. */
export default function ZohoLayoutSection({
  section,
  fields,
  totalFieldCount,
  sectionNames,
  collapsed,
  searching,
  config,
  canManage,
  onToggle,
  onAddField,
  onRename,
  onAction,
}) {
  const visibleFields = fields || [];
  const total = totalFieldCount ?? (section.fields || []).length;
  const shown = visibleFields.length;
  return (
    <div className="sg-zh-section">
      <div className="sg-zh-sect">
        <button
          type="button"
          className={`btn-ghost sg-zh-sect-toggle ${collapsed ? "is-collapsed" : ""}`}
          onClick={onToggle}
          disabled={searching}
          aria-expanded={!collapsed}
          aria-label={`${collapsed ? "Expand" : "Collapse"} ${section.name}`}
          title={searching ? "Clear the field search before collapsing sections" : ""}
        >
          <IconChevronDown />
        </button>
        <h4>{section.name}</h4>
        <span className="sg-zh-scount">
          {searching ? `${shown} of ${total}` : total} field{total === 1 ? "" : "s"}
        </span>
        <span className="sg-zh-sect-line" aria-hidden="true" />
        {canManage ? (
          <>
            <button
              type="button"
              className="secondary sg-zh-sectadd"
              onClick={() => onAddField(section.name)}
            >
              + Add field
            </button>
            <ZohoActionMenu
              label={`Actions for section ${section.name}`}
              items={[{ key: "rename", label: "Rename section" }]}
              onAction={() => onRename(section)}
            />
          </>
        ) : null}
      </div>
      {!collapsed && visibleFields.length ? (
        <div className="sg-zh-fgrid">
          {visibleFields.map((field) => (
            <ZohoFieldCard
              key={field.id || field.apiName}
              field={field}
              role={fieldRole(field, config)}
              canManage={canManage}
              sectionName={section.name}
              sectionNames={sectionNames}
              onAction={onAction}
            />
          ))}
        </div>
      ) : !collapsed && !searching ? (
        <p className="muted">No fields in this section yet.</p>
      ) : null}
    </div>
  );
}
