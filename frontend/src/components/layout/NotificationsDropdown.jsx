import { formatAlertTime, getAlertResourceName } from "../../lib/alertDisplay.js";

function severityPillClass(severity) {
  const value = String(severity || "").toLowerCase();
  if (value === "critical") {
    return "danger";
  }
  if (value === "warning") {
    return "warn";
  }
  return "info";
}

export default function NotificationsDropdown({
  open,
  alerts = [],
  clusterLabel,
  canViewAlerts,
  notificationsEnabled,
  onViewAllAlerts,
  onClose,
}) {
  if (!open) {
    return null;
  }

  const firingAlerts = alerts.filter((alert) => String(alert.status || "").toLowerCase() !== "resolved");

  return (
    <div className="notifications-dropdown" role="dialog" aria-label="Notifications">
      <div className="notifications-dropdown__header">
        <div>
          <h3>Notifications</h3>
          {clusterLabel ? <p className="muted notifications-dropdown__scope">{clusterLabel}</p> : null}
        </div>
        <button type="button" className="modal-close notifications-dropdown__close" onClick={onClose} aria-label="Close notifications">
          ×
        </button>
      </div>

      <div className="notifications-dropdown__body">
        {!canViewAlerts ? (
          <p className="muted notifications-dropdown__empty">You do not have permission to view alerts.</p>
        ) : !notificationsEnabled ? (
          <p className="muted notifications-dropdown__empty">Alert notifications are turned off in Settings.</p>
        ) : firingAlerts.length === 0 ? (
          <p className="muted notifications-dropdown__empty">No firing alerts for this cluster.</p>
        ) : (
          <ul className="notifications-list">
            {firingAlerts.map((alert) => (
              <li key={alert.id} className="notifications-list__item">
                <div className="notifications-list__meta">
                  <span className={`status-pill ${severityPillClass(alert.severity)}`}>
                    {alert.severity || "info"}
                  </span>
                  <time className="notifications-list__time" dateTime={alert.firedAt}>
                    {formatAlertTime(alert.firedAt)}
                  </time>
                </div>
                <p className="notifications-list__title">{alert.title || alert.description || "Alert"}</p>
                <p className="muted notifications-list__detail">
                  {alert.namespace ? `${alert.namespace} · ` : ""}
                  {getAlertResourceName(alert)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </div>

      {canViewAlerts ? (
        <div className="notifications-dropdown__footer">
          <button type="button" className="btn-outline notifications-dropdown__view-all" onClick={onViewAllAlerts}>
            View all alerts
          </button>
        </div>
      ) : null}
    </div>
  );
}
