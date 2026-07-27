import ZohoFieldCard from "./ZohoFieldCard.jsx";
import { fieldRole } from "./zohoFieldMeta";

/** One layout section: its rule-and-label header plus the grid of field cards. */
export default function ZohoLayoutSection({ section, config, canManage, onAddField, onAction }) {
  const fields = section.fields || [];
  return (
    <div>
      <h4 className="sg-zh-sect">
        <span>{section.name}</span>
        <span className="sg-zh-scount">
          {fields.length} field{fields.length === 1 ? "" : "s"}
        </span>
        {canManage ? (
          <button
            type="button"
            className="link-button sg-zh-sectadd"
            onClick={() => onAddField(section.name)}
          >
            + Add field here
          </button>
        ) : null}
      </h4>
      {fields.length ? (
        <div className="sg-zh-fgrid">
          {fields.map((field) => (
            <ZohoFieldCard
              key={field.id || field.apiName}
              field={field}
              role={fieldRole(field, config)}
              canManage={canManage}
              onAction={onAction}
            />
          ))}
        </div>
      ) : (
        <p className="muted">No fields in this section yet.</p>
      )}
    </div>
  );
}
