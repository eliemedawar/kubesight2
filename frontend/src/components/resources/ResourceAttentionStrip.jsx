import { memo } from "react";

/**
 * The few rows in this namespace that need a look, worst first. Each one
 * opens its kind with the "Needs attention" filter on.
 *
 * items: [{ key, tabKey, kindTitle, name, status, tone }]
 */
function ResourceAttentionStrip({ items, total, onOpen }) {
  if (!items.length) {
    return null;
  }
  const more = Math.max(0, total - items.length);
  return (
    <section className="resources-attention" aria-label="Needs attention">
      <span className="resources-attention__label">Needs attention</span>
      <ul className="resources-attention__list">
        {items.map((item) => (
          <li key={item.key}>
            <button
              type="button"
              className="resources-attention__item"
              onClick={() => onOpen(item)}
            >
              <span
                className={`resources-attention__dot resources-attention__dot--${item.tone}`}
                aria-hidden="true"
              />
              <span className="resources-attention__name">{item.name}</span>
              <span className="resources-attention__status">{item.status}</span>
              <span className="resources-attention__kind">{item.kindTitle}</span>
            </button>
          </li>
        ))}
      </ul>
      {more ? <span className="resources-attention__more">+{more} more</span> : null}
    </section>
  );
}

export default memo(ResourceAttentionStrip);
