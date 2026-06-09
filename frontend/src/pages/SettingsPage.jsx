import PageTitle from "../components/common/PageTitle.jsx";
import InfoCard from "../components/common/InfoCard.jsx";

export default function SettingsPage({
  data,
  clusters,
  settingsDraft,
  onSettingsChange,
  onSave,
  saving,
  canManage,
  onNavigateToAlertRouting,
  canManageAlertRouting,
}) {
  const clusterOptions = clusters || [];
  const readOnly = !canManage;
  return (
    <>
      <PageTitle
        title="Settings"
        subtitle="Manage environment defaults and notification preferences."
      />
      <section className="content-grid">
        <InfoCard title="Profile">
          <p>{data.user.name}</p>
          <p className="muted">{data.user.role}</p>
          <p className="muted">{data.user.org || "Organization unavailable"}</p>
        </InfoCard>
        <InfoCard title="Preferences">
          <div className="settings-form">
            <label>
              Theme
              <select
                value={settingsDraft.theme}
                disabled={readOnly}
                onChange={(event) => onSettingsChange("theme", event.target.value)}
              >
                <option value="system">System</option>
                <option value="dark">Dark</option>
                <option value="light">Light</option>
              </select>
            </label>
            <label>
              Refresh Interval (seconds)
              <input
                type="number"
                min={5}
                step={5}
                disabled={readOnly}
                value={settingsDraft.refreshIntervalSeconds}
                onChange={(event) =>
                  onSettingsChange("refreshIntervalSeconds", Number(event.target.value) || 30)
                }
              />
            </label>
            <label>
              Default Cluster
              <select
                value={settingsDraft.defaultCluster}
                disabled={readOnly}
                onChange={(event) => onSettingsChange("defaultCluster", event.target.value)}
              >
                {clusterOptions.map((cluster) => (
                  <option key={cluster.id} value={cluster.id}>
                    {cluster.name}
                  </option>
                ))}
                {!clusterOptions.length ? <option value="">No clusters available</option> : null}
              </select>
            </label>
            {canManageAlertRouting ? (
              <p className="muted settings-hint">
                Configure SMTP and notification receivers in{" "}
                <button type="button" className="link-button" onClick={onNavigateToAlertRouting}>
                  Alert Routing
                </button>
                .
              </p>
            ) : (
              <p className="muted settings-hint">
                Alert routing and notification channels are managed by administrators.
              </p>
            )}
            <label className="checkbox-label settings-checkbox">
              <input
                type="checkbox"
                disabled={readOnly}
                checked={settingsDraft.notifications.alerts}
                onChange={(event) => onSettingsChange("notifications.alerts", event.target.checked)}
              />
              Alerts notifications (master toggle)
            </label>
            <label className="checkbox-label settings-checkbox">
              <input
                type="checkbox"
                disabled={readOnly}
                checked={settingsDraft.notifications.upgrades}
                onChange={(event) => onSettingsChange("notifications.upgrades", event.target.checked)}
              />
              Upgrade notifications
            </label>
          </div>
        </InfoCard>
      </section>
      {canManage ? (
        <section className="card settings-actions">
          <button type="button" onClick={onSave} disabled={saving}>
            Save Settings
          </button>
        </section>
      ) : (
        <p className="muted settings-hint">You can view settings but cannot save changes.</p>
      )}
    </>
  );
}
