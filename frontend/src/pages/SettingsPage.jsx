import { useEffect, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import SearchableSelect from "../components/common/SearchableSelect.jsx";
import { getUserInitials, normalizeSettings } from "../utils/formatters.js";
import { setAlertsTabHint } from "../lib/alertDisplay.js";
import { isAdminUser } from "../utils/authz.js";

const REFRESH_PRESETS = [
  { value: 15, label: "15s" },
  { value: 30, label: "30s" },
  { value: 60, label: "60s" },
  { value: 300, label: "5m" },
];

const ICONS = {
  profile: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-6-3a2 2 0 11-4 0 2 2 0 014 0zm-2 4a5 5 0 00-4.546 2.916A5.986 5.986 0 0010 16a5.986 5.986 0 004.546-2.084A5 5 0 0010 11z" clipRule="evenodd" />
    </svg>
  ),
  appearance: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M10 2a1 1 0 011 1v1a1 1 0 11-2 0V3a1 1 0 011-1zm4 8a4 4 0 11-8 0 4 4 0 018 0zm-.464 4.95l.707.707a1 1 0 001.414-1.414l-.707-.707a1 1 0 00-1.414 1.414zm2.12-10.607a1 1 0 010 1.414l-.706.707a1 1 0 11-1.414-1.414l.707-.707a1 1 0 011.414 0zM17 11a1 1 0 100-2h-1a1 1 0 100 2h1zm-7 4a1 1 0 011 1v1a1 1 0 11-2 0v-1a1 1 0 011-1zM5.05 6.464A1 1 0 106.465 5.05l-.708-.707a1 1 0 00-1.414 1.414l.707.707zm1.414 8.486l-.707.707a1 1 0 01-1.414-1.414l.707-.707a1 1 0 011.414 1.414zM4 11a1 1 0 100-2H3a1 1 0 000 2h1z" />
    </svg>
  ),
  workspace: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M5 4a1 1 0 00-2 0v7.268a2 2 0 000 3.464V16a1 1 0 102 0v-1.268a2 2 0 000-3.464V4zM11 4a1 1 0 10-2 0v1.268a2 2 0 000 3.464V16a1 1 0 102 0V8.732a2 2 0 000-3.464V4zM16 3a1 1 0 011 1v7.268a2 2 0 010 3.464V16a1 1 0 11-2 0v-1.268a2 2 0 010-3.464V4a1 1 0 011-1z" />
    </svg>
  ),
  notifications: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M10 2a6 6 0 00-6 6v3.586l-.707.707C2.663 12.923 3.109 14 4 14h12c.891 0 1.337-1.077.707-1.707L16 11.586V8a6 6 0 00-6-6zM10 18a3 3 0 01-3-3h6a3 3 0 01-3 3z" />
    </svg>
  ),
  security: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M2.166 4.999A11.954 11.954 0 0010 1.944 11.954 11.954 0 0017.834 5c.11.65.166 1.32.166 2.001 0 5.225-3.34 9.67-8 11.317C5.34 16.67 2 12.225 2 7c0-.682.057-1.35.166-2.001zm11.541 3.708a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
    </svg>
  ),
  lock: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clipRule="evenodd" />
    </svg>
  ),
  users: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M9 6a3 3 0 11-6 0 3 3 0 016 0zM17 6a3 3 0 11-6 0 3 3 0 016 0zM12.93 17c.046-.327.07-.66.07-1a6.97 6.97 0 00-1.5-4.33A5 5 0 0119 16v1h-6.07zM6 11a5 5 0 015 5v1H1v-1a5 5 0 015-5z" />
    </svg>
  ),
  routing: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M12.586 4.586a2 2 0 112.828 2.828l-3 3a2 2 0 01-2.828 0 1 1 0 00-1.414 1.414 4 4 0 005.656 0l3-3a4 4 0 00-5.656-5.656l-1.5 1.5a1 1 0 101.414 1.414l1.5-1.5zm-5 5a2 2 0 012.828 0 1 1 0 101.414-1.414 4 4 0 00-5.656 0l-3 3a4 4 0 105.656 5.656l1.5-1.5a1 1 0 10-1.414-1.414l-1.5 1.5a2 2 0 11-2.828-2.828l3-3z" clipRule="evenodd" />
    </svg>
  ),
  external: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path d="M11 3a1 1 0 100 2h2.586l-6.293 6.293a1 1 0 101.414 1.414L15 6.414V9a1 1 0 102 0V4a1 1 0 00-1-1h-5z" />
      <path d="M5 5a2 2 0 00-2 2v8a2 2 0 002 2h8a2 2 0 002-2v-3a1 1 0 10-2 0v3H5V7h3a1 1 0 000-2H5z" />
    </svg>
  ),
  chevron: (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path fillRule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clipRule="evenodd" />
    </svg>
  ),
};

const SECTIONS = [
  { id: "settings-profile", label: "Profile", icon: ICONS.profile },
  { id: "settings-appearance", label: "Appearance", icon: ICONS.appearance },
  { id: "settings-workspace", label: "Workspace", icon: ICONS.workspace },
  { id: "settings-notifications", label: "Notifications", icon: ICONS.notifications },
  { id: "settings-security", label: "Security", icon: ICONS.security },
];

const ADMIN_LINKS = [
  // Alert Routing lives as an admin-gated tab inside the Alerts section.
  { key: "alerts", label: "Alert Routing", adminOnly: true, alertsTab: "routing" },
  { key: "userManagement", label: "User Management" },
  { key: "imageRegistries", label: "Image Registries" },
];

const THEME_OPTIONS = [
  { value: "light", label: "Light", hint: "Signal default — paper & ink" },
  { value: "dark", label: "Dark", hint: "Warm ink, same red voice" },
  { value: "system", label: "System", hint: "Follows your OS preference" },
];

function ScopeChip({ scope }) {
  if (scope === "workspace") {
    return (
      <span className="scope-chip scope-chip--workspace" title="Applies to every user in this workspace">
        {ICONS.users}
        Workspace — applies to everyone
      </span>
    );
  }
  return <span className="scope-chip scope-chip--personal">Personal</span>;
}

function SettingsCard({ id, title, subtitle, scope, locked, children }) {
  return (
    <section className={`settings-card${locked ? " is-locked" : ""}`} id={id} aria-labelledby={`${id}-title`}>
      <div className="settings-card-head">
        <h3 id={`${id}-title`}>{title}</h3>
        <div className="settings-card-chips">
          {locked ? (
            <span className="scope-chip scope-chip--locked">
              {ICONS.lock}
              View only
            </span>
          ) : null}
          <ScopeChip scope={scope} />
        </div>
      </div>
      <p className="settings-card-sub">{subtitle}</p>
      {children}
    </section>
  );
}

function ToggleSwitch({ checked, disabled, onChange, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="settings-switch"
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

function ThemeTilePreview({ value }) {
  return (
    <span className={`theme-tile-preview theme-tile-preview--${value}`} aria-hidden="true">
      <span className="tp-side">
        <span className="tp-line tp-line-active" style={{ width: "85%" }} />
        <span className="tp-line" style={{ width: "70%" }} />
        <span className="tp-line" style={{ width: "75%" }} />
      </span>
      <span className="tp-body">
        <span className="tp-card" />
        <span className="tp-card" />
      </span>
    </span>
  );
}

function formatLastSignIn(isoValue) {
  if (!isoValue) return "No previous sign-in recorded";
  // Backend serializes naive UTC without a Z suffix; append it so the
  // browser converts to local time instead of misreading it as local.
  const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(isoValue) ? isoValue : `${isoValue}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return isoValue;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

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
}) {
  const clusterOptions = clusters || [];
  // Normalize the saved copy: before core data loads it is an empty object.
  const saved = normalizeSettings(data.settings);
  const [activeSection, setActiveSection] = useState(SECTIONS[0].id);

  // Scrollspy: highlight the rail link of the section nearest the top.
  useEffect(() => {
    const cards = SECTIONS.map((section) => document.getElementById(section.id)).filter(Boolean);
    if (!cards.length || typeof IntersectionObserver === "undefined") return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) setActiveSection(entry.target.id);
        });
      },
      { rootMargin: "-15% 0px -70% 0px" }
    );
    cards.forEach((card) => observer.observe(card));
    return () => observer.disconnect();
  }, []);

  const scrollToSection = (id) => {
    setActiveSection(id);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
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

  const refreshValue = Number(settingsDraft.refreshIntervalSeconds) || 30;
  const refreshOptions = REFRESH_PRESETS.some((preset) => preset.value === refreshValue)
    ? REFRESH_PRESETS
    : [...REFRESH_PRESETS, { value: refreshValue, label: `${refreshValue}s` }].sort(
        (a, b) => a.value - b.value
      );

  const alertsEnabled = Boolean(settingsDraft.notifications.alerts);
  const canOpenAlertRouting = isAdminUser(authUser) && isPageAllowed?.("alerts");
  const adminLinks = ADMIN_LINKS.filter(
    (link) => isPageAllowed?.(link.key) && (!link.adminOnly || isAdminUser(authUser))
  );
  const mfaEnabled = Boolean(authUser?.mfaEnabled);

  return (
    <>
      <PageTitle
        title="Settings"
        subtitle="Personal preferences, workspace defaults, and account security."
      />

      {!canManage ? (
        <div className="settings-viewer-banner" role="note">
          {ICONS.lock}
          <span>
            <strong>View only.</strong> Your role can change personal preferences, but workspace
            settings are managed by administrators.
          </span>
        </div>
      ) : null}

      <div className="settings-layout">
        <nav className="settings-rail" aria-label="Settings sections">
          {SECTIONS.map((section) => (
            <button
              key={section.id}
              type="button"
              className={`settings-rail-link${activeSection === section.id ? " active" : ""}`}
              onClick={() => scrollToSection(section.id)}
            >
              {section.icon}
              {section.label}
            </button>
          ))}
          {adminLinks.length ? (
            <>
              <p className="settings-rail-divider" aria-hidden="true">Administration</p>
              {adminLinks.map((link) => (
                <button
                  key={`${link.key}-${link.label}`}
                  type="button"
                  className="settings-rail-ext"
                  onClick={() => {
                    if (link.alertsTab) {
                      setAlertsTabHint(link.alertsTab);
                    }
                    onNavigate?.(link.key);
                  }}
                >
                  {link.label}
                  {ICONS.external}
                </button>
              ))}
            </>
          ) : null}
        </nav>

        <div className="settings-sections">
          <SettingsCard
            id="settings-profile"
            title="Profile"
            subtitle="Your identity across KubeSight. Shown on requests, approvals, and audit entries."
            scope="personal"
          >
            <div className="settings-profile-row">
              <div className="settings-avatar" aria-hidden="true">
                {getUserInitials(data.user.name)}
              </div>
              <div>
                <div className="settings-profile-name">
                  <strong>{data.user.name}</strong>
                  {data.user.role ? (
                    <span className="status-pill settings-role-pill">{data.user.role}</span>
                  ) : null}
                </div>
                <div className="settings-profile-meta">
                  {authUser?.username ? <span className="mono">{authUser.username}</span> : null}
                  {authUser?.email ? <span>{authUser.email}</span> : null}
                </div>
              </div>
            </div>
            <p className="settings-card-footnote">
              {ICONS.lock}
              <span>
                Name, role, and email are managed in{" "}
                {isPageAllowed?.("userManagement") ? (
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => onNavigate?.("userManagement")}
                  >
                    User Management
                  </button>
                ) : (
                  "User Management by your administrator"
                )}
                .
              </span>
            </p>
          </SettingsCard>

          <SettingsCard
            id="settings-appearance"
            title="Appearance"
            subtitle="Applies to this browser only — it never changes what teammates see."
            scope="personal"
          >
            <div className="theme-tiles" role="radiogroup" aria-label="Theme">
              {THEME_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={settingsDraft.theme === option.value}
                  className="theme-tile"
                  onClick={() => onSettingsChange("theme", option.value)}
                >
                  <ThemeTilePreview value={option.value} />
                  <span className="theme-tile-label">
                    <span className="theme-tile-radio" aria-hidden="true" />
                    {option.label}
                  </span>
                  <p className="theme-tile-hint">{option.hint}</p>
                </button>
              ))}
            </div>
          </SettingsCard>

          <SettingsCard
            id="settings-workspace"
            title="Workspace defaults"
            subtitle="Shared defaults every session starts from. Changes here affect all users."
            scope="workspace"
            locked={!canManage}
          >
            <div className="settings-lockable">
              <div className="settings-field">
                <label htmlFor="settings-default-cluster">Default cluster</label>
                <SearchableSelect
                  id="settings-default-cluster"
                  value={settingsDraft.defaultCluster}
                  disabled={!canManage}
                  onChange={(event) => onSettingsChange("defaultCluster", event.target.value)}
                >
                  {clusterOptions.map((cluster) => (
                    <option key={cluster.id} value={cluster.id}>
                      {cluster.name}
                    </option>
                  ))}
                  {!clusterOptions.length ? <option value="">No clusters available</option> : null}
                </SearchableSelect>
                <p className="settings-field-hint">
                  Pre-selected in the cluster switcher when someone signs in.
                </p>
              </div>
              <div className="settings-field">
                <span className="settings-field-label" id="settings-refresh-label">
                  Refresh interval
                </span>
                <div className="settings-segmented" role="group" aria-labelledby="settings-refresh-label">
                  {refreshOptions.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      aria-pressed={refreshValue === option.value}
                      disabled={!canManage}
                      onClick={() => onSettingsChange("refreshIntervalSeconds", option.value)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <p className="settings-field-hint">
                  How often dashboards and tables poll the clusters. Shorter intervals increase
                  API-server load.
                </p>
              </div>
            </div>
          </SettingsCard>

          <SettingsCard
            id="settings-notifications"
            title="Notifications"
            subtitle="Master switches for outbound email. Who receives what is defined in Alert Routing."
            scope="workspace"
            locked={!canManage}
          >
            <div className="settings-lockable">
              <div className="settings-toggle-row">
                <div className="settings-toggle-text">
                  <p className="settings-toggle-title">Alert notifications</p>
                  <p className="settings-toggle-desc">
                    Fire and resolve emails for triggered alert policies — cluster, node, and
                    service health.
                  </p>
                </div>
                <ToggleSwitch
                  checked={alertsEnabled}
                  disabled={!canManage}
                  label="Alert notifications"
                  onChange={(value) => onSettingsChange("notifications.alerts", value)}
                />
              </div>
              <div className={`settings-toggle-row child${alertsEnabled ? "" : " dimmed"}`}>
                <div className="settings-toggle-text">
                  <p className="settings-toggle-title">Upgrade notifications</p>
                  <p className="settings-toggle-desc">
                    Version-upgrade windows, deployment outcomes, and rollback events.
                  </p>
                </div>
                <ToggleSwitch
                  checked={Boolean(settingsDraft.notifications.upgrades)}
                  disabled={!canManage || !alertsEnabled}
                  label="Upgrade notifications"
                  onChange={(value) => onSettingsChange("notifications.upgrades", value)}
                />
              </div>
            </div>
            {canOpenAlertRouting ? (
              <button
                type="button"
                className="settings-link-row"
                onClick={() => {
                  setAlertsTabHint("routing");
                  onNavigate?.("alerts");
                }}
              >
                {ICONS.routing}
                <span>
                  <span className="lr-title">Alert Routing</span>
                  <span className="lr-desc"> — SMTP server, receiver groups, and escalation policies</span>
                </span>
                <span className="lr-chevron">{ICONS.chevron}</span>
              </button>
            ) : (
              <p className="settings-card-footnote">
                {ICONS.lock}
                <span>Alert routing and notification channels are managed by administrators.</span>
              </p>
            )}
          </SettingsCard>

          <SettingsCard
            id="settings-security"
            title="Security"
            subtitle="How your account signs in. Two-factor codes were set up during your first login."
            scope="personal"
          >
            <div className="settings-info-row">
              <div className="settings-toggle-text">
                <p className="settings-toggle-title">
                  Two-factor authentication
                  <span className={`status-pill ${mfaEnabled ? "ok" : "warn"}`}>
                    {mfaEnabled ? "Enabled" : "Not set up"}
                  </span>
                </p>
                <p className="settings-toggle-desc">
                  Time-based one-time codes from your authenticator app, required at every sign-in.
                </p>
              </div>
            </div>
            <div className="settings-info-row">
              <div className="settings-toggle-text">
                <p className="settings-toggle-title">Last sign-in</p>
                <p className="settings-toggle-desc">
                  {formatLastSignIn(authUser?.lastLoginAt)}
                  {authUser?.lastLoginIp ? (
                    <>
                      {" from "}
                      <span className="mono">{authUser.lastLoginIp}</span>
                    </>
                  ) : null}
                </p>
              </div>
            </div>
            <p className="settings-card-footnote">
              {ICONS.lock}
              <span>
                Password resets and authenticator rebuilds are handled by an administrator in User
                Management.
              </span>
            </p>
          </SettingsCard>
        </div>
      </div>

      {canManage && dirtySections.length ? (
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
