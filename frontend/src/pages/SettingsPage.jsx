import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { pathForPageKey } from "../routes/paths.js";
import PageTitle from "../components/common/PageTitle.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import { normalizeSettings } from "../utils/formatters.js";
import { isAdminUser } from "../utils/authz.js";
import {
  groupSettingsSections,
  PREFERENCE_ANCHORS,
  visibleSettingsSections,
} from "../lib/settingsSections.js";
import ICONS from "./settings/settingsIcons.jsx";
import PreferencesPanel from "./settings/PreferencesPanel.jsx";

/**
 * Settings — the one place things get configured.
 *
 * The rail lists every section the signed-in user may touch, grouped into
 * Preferences / Integrations / Administration; the column beside it renders
 * exactly one of them. What belongs in the rail lives in
 * `lib/settingsSections.js`, not here, so adding an integration is one entry
 * plus one panel rather than an edit spread across the nav registry, the page
 * switch, and the sidebar.
 *
 * Preference sections share a single panel and differ only by which card they
 * scroll to — five short cards read better as one column than as five screens.
 * Integrations are a single entry opening a hub of provider cards, so the rail
 * stays the same length as providers are added.
 */

export default function SettingsPage({
  data,
  clusters,
  settingsDraft,
  onSettingsChange,
  onSave,
  onDiscard,
  saving,
  canManage,
  authUser,
  onNavigate,
  isPageAllowed,
  hasPermission = () => false,
}) {
  const saved = normalizeSettings(data.settings);
  const isAdmin = isAdminUser(authUser);

  const sections = useMemo(
    () =>
      visibleSettingsSections({
        hasPermission,
        isAdmin,
        isPageAllowed,
        canViewPreferences: hasPermission("settings:view") || isAdmin,
      }),
    [hasPermission, isAdmin, isPageAllowed]
  );
  const groups = useMemo(() => groupSettingsSections(sections), [sections]);

  // Sections that render a panel; the link-out rows are never "active".
  const panelSections = useMemo(() => sections.filter((section) => !section.link), [sections]);

  // The open section is in the URL: /admin/settings?section=integrationsHub is
  // a link. It was a one-shot sessionStorage hint written by the caller and
  // consumed on mount, which existed only because there was no router.
  const [searchParams, setSearchParams] = useSearchParams();
  const activeId = searchParams.get("section") || "";
  const setActiveId = useCallback(
    (next) => {
      const params = new URLSearchParams(searchParams);
      if (next) {
        params.set("section", next);
      } else {
        params.delete("section");
      }
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams]
  );

  // Resolve the active section once the visible set is known — a hint may point
  // at something this user cannot see, and the fallback differs per role.
  const active =
    panelSections.find((section) => section.id === activeId) || panelSections[0] || null;

  useEffect(() => {
    if (active && active.id !== activeId) {
      setActiveId(active.id);
    }
  }, [active, activeId]);

  // The preference sections share one panel, so picking one is a scroll rather
  // than a panel swap. Only a click scrolls: the scrollspy below also writes
  // activeId, and if that write scrolled too the two would drive each other.
  const scrollToRef = useRef("");
  useEffect(() => {
    const anchor = scrollToRef.current;
    if (!anchor) return;
    scrollToRef.current = "";
    document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // Scrolling that column moves the rail highlight to whichever card is in
  // view. Only the shared preferences panel needs this; every other panel is a
  // single section.
  const isPreferences = active?.panel === "preferences";
  useEffect(() => {
    if (!isPreferences || typeof IntersectionObserver === "undefined") return undefined;
    const anchors = PREFERENCE_ANCHORS.map(({ id, anchor }) => ({
      id,
      node: document.getElementById(anchor),
    })).filter((entry) => entry.node);
    if (!anchors.length) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const match = anchors.find(({ node }) => node === entry.target);
          if (match) setActiveId(match.id);
        });
      },
      { rootMargin: "-15% 0px -70% 0px" }
    );
    anchors.forEach(({ node }) => observer.observe(node));
    return () => observer.disconnect();
  }, [isPreferences]);

  const openSection = (sectionId) => {
    const target = panelSections.find((section) => section.id === sectionId);
    if (target?.anchor) {
      scrollToRef.current = target.anchor;
    }
    setActiveId(sectionId);
  };

  // Link-outs navigate to a real address, tab included, instead of stashing a
  // hint for the destination to pick up.
  const navigate = useNavigate();

  const followLink = (section) => {
    const path = pathForPageKey(section.link);
    if (!path) {
      return;
    }
    navigate(section.alertsTab ? `${path}?tab=${section.alertsTab}` : path);
  };

  // Dirty tracking against the last saved settings. Theme is excluded: it is
  // a personal, per-browser preference that applies immediately.
  const dirtySections = [];
  if (
    settingsDraft.defaultCluster !== saved.defaultCluster ||
    Number(settingsDraft.refreshIntervalSeconds) !== Number(saved.refreshIntervalSeconds)
  ) {
    dirtySections.push("Workspace");
  }
  if (
    Boolean(settingsDraft.notifications.alerts) !== Boolean(saved.notifications.alerts) ||
    Boolean(settingsDraft.notifications.upgrades) !== Boolean(saved.notifications.upgrades)
  ) {
    dirtySections.push("Notifications");
  }

  // The hub is a link-out now, so it is never a panel; the preferences panel
  // still wants to know whether this user can reach it before offering a link.
  const canOpenIntegrations = sections.some((section) => section.id === "integrationsHub");
  // The bar stays up while an integration panel is open: the draft survives the
  // panel switch, and hiding the only way to save it would read as discarded.
  const showSaveBar = canManage && dirtySections.length > 0;

  const renderPanel = () => {
    if (!active) {
      return (
        <p className="muted">
          No settings are available to your account. Contact an administrator.
        </p>
      );
    }

    switch (active.panel) {
      case "preferences":
        return (
          <PreferencesPanel
            user={data.user}
            clusters={clusters}
            settingsDraft={settingsDraft}
            onSettingsChange={onSettingsChange}
            canManage={canManage}
            authUser={authUser}
            onNavigate={onNavigate}
            isPageAllowed={isPageAllowed}
            canOpenIntegrations={canOpenIntegrations}
            onOpenSection={openSection}
          />
        );
      default:
        return null;
    }
  };

  return (
    <>
      <PageTitle
        title="Settings"
        subtitle="Preferences, integrations, and the systems KubeSight connects to."
      />

      <div className={`settings-layout${active?.wide ? " settings-layout--wide" : ""}`}>
        <nav className="settings-rail" aria-label="Settings sections">
          {groups.map((group, index) => (
            <div className="settings-rail-group" key={group.id}>
              {index > 0 || group.id !== "preferences" ? (
                <p className="settings-rail-divider" aria-hidden="true">
                  {group.label}
                </p>
              ) : null}
              {group.sections.map((section) =>
                section.link ? (
                  <button
                    key={section.id}
                    type="button"
                    className="settings-rail-ext"
                    title={section.hint}
                    onClick={() => followLink(section)}
                  >
                    {section.label}
                    {ICONS.external}
                  </button>
                ) : (
                  <button
                    key={section.id}
                    type="button"
                    className={`settings-rail-link${active?.id === section.id ? " active" : ""}`}
                    aria-current={active?.id === section.id ? "true" : undefined}
                    onClick={() => openSection(section.id)}
                  >
                    {ICONS[section.icon]}
                    {section.label}
                  </button>
                )
              )}
            </div>
          ))}
        </nav>

        <div className="settings-panel" key={active?.panel || "empty"}>
          {active?.title ? (
            <header className="settings-panel-head">
              <h3>{active.title}</h3>
              {active.summary ? <p>{active.summary}</p> : null}
            </header>
          ) : null}
          <Suspense fallback={<LoadingState label={`Loading ${active?.label || "settings"}...`} />}>
            {renderPanel()}
          </Suspense>
        </div>
      </div>

      {showSaveBar ? (
        <div className="settings-savebar" role="status">
          <span className="settings-savebar-dot" aria-hidden="true" />
          <span className="settings-savebar-text">
            <strong>Unsaved changes</strong> · {dirtySections.join(", ")}
          </span>
          <button type="button" className="btn-ghost" onClick={onDiscard} disabled={saving}>
            Discard
          </button>
          <button type="button" className="primary" onClick={onSave} disabled={saving}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      ) : null}
    </>
  );
}
