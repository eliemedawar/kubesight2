import SearchableSelect from "../../components/common/SearchableSelect.jsx";
import { getUserInitials } from "../../utils/formatters.js";
import ICONS from "./settingsIcons.jsx";

/* Preferences — the five scoped cards that were the whole Settings page before
   integrations moved in beside them. They stay one scrolling column rather than
   five panels: every card is short, and Personal vs Workspace scope only reads
   as a distinction when you can see both at once. */

const REFRESH_PRESETS = [
  { value: 15, label: "15s" },
  { value: 30, label: "30s" },
  { value: 60, label: "60s" },
  { value: 300, label: "5m" },
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

export default function PreferencesPanel({
  user,
  clusters,
  settingsDraft,
  onSettingsChange,
  canManage,
  authUser,
  onNavigate,
  isPageAllowed,
  canOpenIntegrations = false,
  onOpenSection,
}) {
  const clusterOptions = clusters || [];
  const refreshValue = Number(settingsDraft.refreshIntervalSeconds) || 30;
  const refreshOptions = REFRESH_PRESETS.some((preset) => preset.value === refreshValue)
    ? REFRESH_PRESETS
    : [...REFRESH_PRESETS, { value: refreshValue, label: `${refreshValue}s` }].sort(
        (a, b) => a.value - b.value
      );

  const alertsEnabled = Boolean(settingsDraft.notifications.alerts);
  const mfaEnabled = Boolean(authUser?.mfaEnabled);

  return (
    <>
      {!canManage ? (
        <div className="settings-viewer-banner" role="note">
          {ICONS.lock}
          <span>
            <strong>View only.</strong> Your role can change personal preferences, but workspace
            settings are managed by administrators.
          </span>
        </div>
      ) : null}

      <div className="settings-sections">
        <SettingsCard
          id="settings-profile"
          title="Profile"
          subtitle="Your identity across KubeSight. Shown on requests, approvals, and audit entries."
          scope="personal"
        >
          <div className="settings-profile-row">
            <div className="settings-avatar" aria-hidden="true">
              {getUserInitials(user.name)}
            </div>
            <div>
              <div className="settings-profile-name">
                <strong>{user.name}</strong>
                {user.role ? (
                  <span className="status-pill settings-role-pill">{user.role}</span>
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
          subtitle="Master switches for outbound email. Who receives what is defined by the SMTP, Slack, and webhook integrations."
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
          {canOpenIntegrations ? (
            <button
              type="button"
              className="settings-link-row"
              onClick={() => onOpenSection?.("integrationsHub")}
            >
              {ICONS.routing}
              <span>
                <span className="lr-title">Integrations</span>
                <span className="lr-desc"> — the SMTP server, Slack, and webhooks these emails go through</span>
              </span>
              <span className="lr-chevron">{ICONS.chevron}</span>
            </button>
          ) : (
            <p className="settings-card-footnote">
              {ICONS.lock}
              <span>Delivery settings and notification channels are managed by administrators.</span>
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
    </>
  );
}
