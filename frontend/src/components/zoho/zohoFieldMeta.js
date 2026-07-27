// Shared vocabulary for the Zoho layout editor.
//
// The Environment / Application / Variable fields are special-cased: the sync
// owns their option lists, so the editor offers "Choose namespaces" on one and
// hides the options button on the other two. Those comparisons used to be
// scattered across the render tree, which made every change to the editor a
// chance to regress them. `fieldRole` is now the single place a field id is
// matched against config.

export const VALUE_CHIP_CAP = 8;

// Fallback only — the backend serves the authoritative list on the layout
// response (`creatableTypes`), so the two can no longer drift.
export const CREATABLE_TYPES = [
  "Text",
  "Textarea",
  "Picklist",
  "Number",
  "Decimal",
  "Date",
  "DateTime",
  "Boolean",
  "Email",
  "Phone",
  "URL",
];

// Values are edited one-per-line; `-None-` is managed by the backend.
export const linesToValues = (text) =>
  String(text || "")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && l !== "-None-");

export const valuesToLines = (values) =>
  (values || []).filter((v) => v !== "-None-").join("\n");

// The source kinds the three config-owned fields are synthesized with, mapped
// back to the role that names their affordances.
const LOCKED_KIND_ROLES = {
  namespaces: "environment",
  deployments: "application",
  env_vars: "variable",
};

/**
 * Which editing affordances a field gets.
 *
 * `variable` is deliberately conditional on `syncVariables`: with the variable
 * sync off, the field is an ordinary picklist the operator manages by hand, and
 * hiding "Manage options" would strand it.
 */
export function fieldRole(field, config = {}) {
  const id = String(field?.id ?? "");
  const envId = String(config.environmentFieldId || "");
  const appId = String(config.appFieldId || "");
  const varId = config.syncVariables ? String(config.variableFieldId || "") : "";

  if (id && id === envId) return "environment";
  if (id && id === appId) return "application";
  if (id && id === varId) return "variable";
  const binding = field?.binding;
  // A locked binding is the backend saying the same thing the ids above do — it
  // is authoritative when the config ids are stale or the field was re-pointed.
  // Only while it is actually publishing: a locked-but-off binding (the Variable
  // field with its sync disabled) must stay a hand-managed picklist.
  if (binding?.locked && binding.enabled) {
    return LOCKED_KIND_ROLES[binding.sourceKind] || "picklist";
  }
  // An operator-added binding: the sync owns its options too, but it is editable
  // (unlike the three above, which are configured on the Source tab).
  if (binding && !binding.locked) return "bound";
  if (field?.isPicklist) return "picklist";
  if (field?.type === "Text" || field?.type === "Textarea") return "text";
  return "plain";
}

/** True when the sync owns this field's option list. */
export const isSyncOwned = (role) =>
  role === "environment" || role === "application" || role === "variable" || role === "bound";

/**
 * Footer buttons for a field card, as data rather than a nested ternary.
 * `action` is dispatched by the editor; `null` roles simply get no options button.
 */
export function fieldActions(field, role, canManage) {
  if (!canManage) return [];
  const actions = [];
  if (role === "environment") {
    actions.push({ key: "source", label: "Choose namespaces", variant: "btn-outline" });
  } else if (role === "picklist") {
    actions.push({ key: "options", label: "Manage options", variant: "btn-ghost" });
    actions.push({ key: "bind", label: "Live source", variant: "btn-ghost" });
  } else if (role === "bound") {
    // No manual option editing: the next sync would overwrite it anyway.
    actions.push({ key: "bind", label: "Live source", variant: "btn-outline" });
  } else if (role === "text") {
    actions.push({ key: "convert", label: "Convert to dropdown", variant: "btn-ghost" });
  }
  // application / variable are auto-derived — no option editing, by design.
  actions.push({ key: "edit", label: "Edit", variant: "btn-ghost" });
  return actions;
}

/** Explanatory copy under a sync-owned picklist's value chips. */
export function fieldHint(role, field) {
  if (role === "bound") {
    const binding = field?.binding || {};
    const parent = binding.parentFieldId ? ", filtered by its parent field" : "";
    return `Published on every sync from ${(binding.sourceLabel || "a live source").toLowerCase()}${parent}. Editing the list by hand is pointless — the next sync replaces it.`;
  }
  if (role === "application") {
    return "Auto-derived live from the selected namespaces' deployments — manage it via “Choose namespaces” on the Environment field.";
  }
  if (role === "variable") {
    return "Auto-derived live from the chosen deployments' env variables — the same name across deployments becomes one option, and picking an Application on the ticket narrows the list to that app's variables. Populates on the next sync.";
  }
  return "";
}
