/**
 * Compact multi-select for picking keys from a (possibly long) list.
 * Chosen keys show as removable chips; remaining keys are added from a single
 * dropdown — which stays compact and type-ahead searchable even with many keys.
 */
export default function KeyMultiSelect({ selected = [], available = [], onToggle, ariaLabel }) {
  const remaining = available.filter((k) => !selected.includes(k));
  return (
    <div className="key-multiselect" role="group" aria-label={ariaLabel}>
      {selected.length ? (
        <div className="key-multiselect__chips">
          {selected.map((k) => (
            <span key={k} className="key-multiselect__chip">
              {k}
              <button type="button" onClick={() => onToggle(k)} aria-label={`Remove ${k}`}>
                ×
              </button>
            </span>
          ))}
        </div>
      ) : null}
      {remaining.length ? (
        <select
          className="key-multiselect__add"
          value=""
          onChange={(e) => {
            if (e.target.value) onToggle(e.target.value);
          }}
          aria-label="Add key"
        >
          <option value="">{selected.length ? "+ Add key…" : "Select keys…"}</option>
          {remaining.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      ) : (
        <span className="key-multiselect__all muted">All keys selected</span>
      )}
    </div>
  );
}
