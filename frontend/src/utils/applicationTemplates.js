// Built-in categories always render in this order. "Custom" is pinned last.
export const TEMPLATE_CATEGORIES = ["Web", "Database", "Messaging", "Observability", "Custom"];

const BUILT_IN_ORDER = TEMPLATE_CATEGORIES.filter((category) => category !== "Custom");

/**
 * Resolve the ordered list of categories to render: the built-in ones first,
 * then any custom categories present on the supplied templates (alphabetical),
 * with "Custom" always last.
 */
export function resolveCategoryOrder(templates = []) {
  const extras = new Set();
  for (const template of templates) {
    const category = (template.category || "").trim();
    if (category && category !== "Custom" && !BUILT_IN_ORDER.includes(category)) {
      extras.add(category);
    }
  }
  return [...BUILT_IN_ORDER, ...[...extras].sort((a, b) => a.localeCompare(b)), "Custom"];
}

export function groupTemplatesByCategory(templates = []) {
  const categories = resolveCategoryOrder(templates);
  const grouped = Object.fromEntries(categories.map((category) => [category, []]));
  for (const template of templates) {
    const category = (template.category || "").trim();
    const bucket = category && grouped[category] ? category : "Custom";
    grouped[bucket].push(template);
  }
  return grouped;
}
