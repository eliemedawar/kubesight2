import { memo } from "react";

function GlobeIcon({ title }) {
  return (
    <svg
      className="resources-rail__globe"
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      role="img"
      aria-label={title}
    >
      <title>{title}</title>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18" />
    </svg>
  );
}

/**
 * The Resources page's kind picker: kinds grouped by what they do, each with
 * its count and, when some of its rows need attention, how many.
 *
 * groups: [{ key, label, items: [{ tabKey, title, count, issues, tone, clusterScoped }] }]
 * `count` is null while unknown (shown blank, never as 0).
 */
function optionLabel(item) {
  const count = item.count == null ? "" : ` (${item.count})`;
  const issues = item.issues ? ` · ${item.issues} need${item.issues === 1 ? "s" : ""} attention` : "";
  return `${item.title}${count}${issues}`;
}

function ResourceKindRail({ groups, activeTab, onSelect }) {
  return (
    <>
      {/* Phones: the same choice as one grouped dropdown instead of a tall list. */}
      <label className="resources-rail-select">
        <span className="resources-rail-select__label">Kind</span>
        <select value={activeTab} onChange={(event) => onSelect(event.target.value)}>
          {groups.map((group) => (
            <optgroup key={group.key} label={group.label}>
              {group.items.map((item) => (
                <option key={item.tabKey} value={item.tabKey}>
                  {optionLabel(item)}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
      <RailList groups={groups} activeTab={activeTab} onSelect={onSelect} />
    </>
  );
}

function RailList({ groups, activeTab, onSelect }) {
  return (
    <nav className="resources-rail" aria-label="Resource kinds">
      {groups.map((group) => (
        <div key={group.key} className="resources-rail__group">
          <p className="resources-rail__label" id={`resources-rail-${group.key}`}>
            {group.label}
          </p>
          <ul className="resources-rail__items" aria-labelledby={`resources-rail-${group.key}`}>
            {group.items.map((item) => {
              const active = item.tabKey === activeTab;
              const issueLabel = item.issues
                ? `${item.issues} ${item.issues === 1 ? "needs" : "need"} attention`
                : "";
              return (
                <li key={item.tabKey}>
                  <button
                    type="button"
                    className={`resources-rail__item${active ? " is-active" : ""}`}
                    aria-current={active ? "page" : undefined}
                    onClick={() => onSelect(item.tabKey)}
                  >
                    <span className="resources-rail__title">{item.title}</span>
                    {item.clusterScoped ? <GlobeIcon title="Cluster-wide" /> : null}
                    {item.issues ? (
                      <span
                        className={`resources-rail__badge resources-rail__badge--${item.tone || "warn"}`}
                        title={issueLabel}
                      >
                        <span aria-hidden="true">{item.issues}</span>
                        <span className="resources-sr-only">{issueLabel}</span>
                      </span>
                    ) : null}
                    <span className="resources-rail__count">
                      {item.count == null ? "" : item.count}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

export default memo(ResourceKindRail);
