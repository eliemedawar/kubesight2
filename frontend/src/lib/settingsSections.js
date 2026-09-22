/**
 * The Settings registry — one list describing everything configurable in
 * KubeSight, grouped into preferences and administration links:
 *
 *   Preferences    what this browser and this workspace default to
 *   Administration the people-and-history pages, which stay their own routes
 *
 * Connecting to an outside system used to happen in whichever screen happened
 * to use it: registries had their own sidebar entry, SMTP and receivers hid
 * behind an admin-only tab on the Alerts page, Jira and Zoho were configured
 * from inside the Ticketing workspace. Those connections now live in the
 * top-level Integrations workspace.
 *
 * The split is connection versus use. The hub owns connections. Working with
 * what a connection enables — browsing tickets, running a sync, analysing a
 * repository — stays on its own page, because that is a job rather than a
 * setting.
 *
 * Each link declares its own RBAC gate rather than relying on the page-level
 * `settings:view` check.
 */

/** Rail groups, in render order. */
export const SETTINGS_GROUPS = [
  { id: "preferences", label: "Preferences" },
  { id: "administration", label: "Administration" },
];

/**
 * Sections, in render order within their group.
 *
 *   panel   which panel component renders the section. Several preference
 *           entries share the `preferences` panel and differ only by `anchor`,
 *           so the rail can list them individually while they stay one
 *           scrollable column of cards.
 *   anchor  DOM id of the card to scroll to inside a shared panel.
 *   link    page key to navigate away to — used by Administration, whose
 *           pages are full workspaces rather than settings forms.
 *   wide    the panel wants the full content column (tables, not forms).
 *   requires  RBAC gate, resolved by visibleSettingsSections().
 */
export const SETTINGS_SECTIONS = [
  // ─── Preferences ───
  {
    id: "profile",
    group: "preferences",
    label: "Profile",
    icon: "profile",
    panel: "preferences",
    anchor: "settings-profile",
  },
  {
    id: "appearance",
    group: "preferences",
    label: "Appearance",
    icon: "appearance",
    panel: "preferences",
    anchor: "settings-appearance",
  },
  {
    id: "workspace",
    group: "preferences",
    label: "Workspace",
    icon: "workspace",
    panel: "preferences",
    anchor: "settings-workspace",
  },
  {
    id: "notifications",
    group: "preferences",
    label: "Notifications",
    icon: "notifications",
    panel: "preferences",
    anchor: "settings-notifications",
  },
  {
    id: "security",
    group: "preferences",
    label: "Security",
    icon: "security",
    panel: "preferences",
    anchor: "settings-security",
  },

  // ─── Administration ───
  {
    // The one administration entry that is a PANEL rather than a link-out: the
    // quality gate is a number, not a workspace, and sending somebody to a page
    // of their own to type one field would be the wrong shape. It is here
    // rather than on a service because it is the installation's policy —
    // raising the bar is one edit, not one per repository.
    id: "mergeChecks",
    group: "administration",
    label: "Merge checks",
    icon: "mergeChecks",
    panel: "mergeChecks",
    title: "Merge checks",
    summary:
      "The quality gate a pull request has to clear before KubeSight will let it merge.",
    requires: { anyPermissions: ["ci_merge_checks:view", "ci_merge_checks:manage"] },
  },
  {
    id: "userManagement",
    group: "administration",
    label: "User management",
    icon: "users",
    link: "userManagement",
  },
  {
    id: "alertPolicies",
    group: "administration",
    label: "Alert policies",
    icon: "policies",
    link: "alerts",
    alertsTab: "policies",
  },
  {
    id: "auditLogs",
    group: "administration",
    label: "Audit logs",
    icon: "audit",
    link: "auditLogs",
  },
];

/** The cards the preferences panel scrolls between, in document order. */
export const PREFERENCE_ANCHORS = SETTINGS_SECTIONS.filter(
  (section) => section.panel === "preferences" && section.anchor
).map((section) => ({ id: section.id, anchor: section.anchor }));

function gateAllows(requires, { hasPermission, isAdmin }) {
  if (!requires) {
    return true;
  }
  if (requires.adminOnly) {
    return Boolean(isAdmin);
  }
  if (requires.anyPermissions?.length) {
    return requires.anyPermissions.some((key) => hasPermission(key));
  }
  if (requires.permission) {
    return hasPermission(requires.permission);
  }
  return true;
}

/**
 * The sections this user may see.
 *
 * `access` carries `hasPermission`, `isAdmin`, and `isPageAllowed` — the same
 * trio the shell already receives from App, so the caller never has to
 * reimplement an RBAC check to decide what the rail shows.
 */
export function visibleSettingsSections({
  hasPermission = () => false,
  isAdmin = false,
  isPageAllowed = () => true,
  canViewPreferences = true,
} = {}) {
  return SETTINGS_SECTIONS.filter((section) => {
    if (section.group === "preferences") {
      return canViewPreferences;
    }
    if (section.link) {
      return isPageAllowed(section.link);
    }
    return gateAllows(section.requires, { hasPermission, isAdmin });
  });
}

/** Rail groups that still have at least one visible section, sections attached. */
export function groupSettingsSections(sections) {
  return SETTINGS_GROUPS.map((group) => ({
    ...group,
    sections: sections.filter((section) => section.group === group.id),
  })).filter((group) => group.sections.length > 0);
}

