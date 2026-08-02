/**
 * The Settings registry — one list describing everything configurable in
 * KubeSight, grouped into the three things an operator is actually doing:
 *
 *   Preferences    what this browser and this workspace default to
 *   Integrations   every outside system KubeSight talks to
 *   Administration the people-and-history pages, which stay their own routes
 *
 * Connecting to an outside system used to happen in whichever screen happened
 * to use it: registries had their own sidebar entry, SMTP and receivers hid
 * behind an admin-only tab on the Alerts page, Jira and Zoho were configured
 * from inside the Ticketing workspace. Those connections all live in the hub
 * now, so "where do I configure X" has one answer.
 *
 * The split is connection versus use. The hub owns connections. Working with
 * what a connection enables — browsing tickets, running a sync, analysing a
 * repository — stays on its own page, because that is a job rather than a
 * setting.
 *
 * Each entry declares its own RBAC gate rather than relying on the page-level
 * `settings:view` check, because a user who may manage registries but not
 * workspace defaults still needs the hub — and nothing else in Settings.
 */

const SECTION_HINT_KEY = "kubesight.settings.section";

/**
 * Anyone who may see at least one integration may open the hub; the hub then
 * shows only the cards that person's permissions cover. Admin-only concerns
 * (SMTP, Slack, webhooks) are filtered server-side, so this list is the union
 * of the non-admin gates plus `settings:view` for the admin case.
 */
export const INTEGRATION_VIEW_PERMISSIONS = [
  "settings:view",
  "ticketing:view",
  "registries:view",
  "applications:view",
];

/** Rail groups, in render order. */
export const SETTINGS_GROUPS = [
  { id: "preferences", label: "Preferences" },
  { id: "integrations", label: "Integrations" },
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

  // ─── Integrations ───
  // One entry, not one per provider. The hub behind it lists every integration
  // as a card and opens a detail screen per provider, so the rail does not have
  // to grow a row each time a provider is added — and so "which of these is
  // broken" is answerable at a glance instead of by clicking through a rail.
  //
  // The hub holds *connections only*. Feature surfaces that sit on top of an
  // integration — browsing tickets, field sync, deploy runs — stay on their own
  // pages; this is where the connection to the outside system is configured,
  // not where the work gets done.
  // The hub is its own top-level address now, not a panel in here. It grew
  // four tabs and a per-provider detail screen, which is a workspace rather
  // than a setting -- and burying it under /admin/settings meant the answer to
  // "is Jira working" was three clicks from anywhere.
  //
  // The rail keeps the row so the path from Settings to it is still obvious.
  {
    id: "integrationsHub",
    group: "integrations",
    label: "All integrations",
    icon: "plug",
    link: "integrations",
    requires: { anyPermissions: INTEGRATION_VIEW_PERMISSIONS },
  },

  // ─── Administration (link-outs) ───
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

/**
 * Deep link into a settings section from elsewhere in the app. Session storage
 * rather than the URL, matching how the rest of the app deep-links — there is
 * no router, so navigation is app state plus a hint the target consumes once.
 */
export function setSettingsSectionHint(sectionId) {
  try {
    window.sessionStorage.setItem(SECTION_HINT_KEY, sectionId);
  } catch {
    /* storage unavailable (private mode) — the link lands on the first section */
  }
}

export function consumeSettingsSectionHint() {
  try {
    const hint = window.sessionStorage.getItem(SECTION_HINT_KEY);
    if (hint) {
      window.sessionStorage.removeItem(SECTION_HINT_KEY);
    }
    return hint || "";
  } catch {
    return "";
  }
}
