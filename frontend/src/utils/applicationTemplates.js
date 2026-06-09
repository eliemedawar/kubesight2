export const TEMPLATE_CATEGORIES = ["Web", "Database", "Messaging", "Observability", "Custom"];

export function groupTemplatesByCategory(templates = []) {
  const grouped = Object.fromEntries(TEMPLATE_CATEGORIES.map((category) => [category, []]));
  for (const template of templates) {
    const category = TEMPLATE_CATEGORIES.includes(template.category) ? template.category : "Custom";
    grouped[category].push(template);
  }
  return grouped;
}
