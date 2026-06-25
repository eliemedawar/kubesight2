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

const REQUEST_STATUS_TONE = {
  approved: "ok",
  declined: "danger",
};

export default function NotificationsDropdown({
  open,
  alerts = [],
  requestUpdates = [],
  canViewRequests = false,
  onViewAllRequests,
  onDismissRequestUpdate,
  onClearRequestUpdates,
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
  const requestItems = canViewRequests ? requestUpdates : [];
  const hasRequestItems = requestItems.length > 0;
  const noPermission = !canViewAlerts && !canViewRequests;

  return (
    <div className="notifications-dropdown" role="dialog" aria-label="Notifications">
      <div className="notifications-dropdown__header">
        <div>
          <h3>Notifications</h3>
          {clusterLabel ? <p className="muted notifications-dropdown__scope">{clusterLabel}</p> : null}
        </div>
        <button type="button" className="modal-close notifications-dropdown__close" onClick={onClose} aria-label="Close notifications">
          <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg>
        </button>
      </div>

      <div className="notifications-dropdown__body">
        {noPermission ? (
          <p className="muted notifications-dropdown__empty">You do not have permission to view notifications.</p>
        ) : (
          <>
            {hasRequestItems ? (
              <div className="notifications-section">
                <div className="notifications-dropdown__section-head">
                  <p className="notifications-dropdown__section-label">Deployment requests</p>
                  <button
                    type="button"
                    className="notifications-dropdown__clear"
                    onClick={onClearRequestUpdates}
                  >
                    Clear all
                  </button>
                </div>
                <ul className="notifications-list">
                  {requestItems.map((req) => (
                    <li key={`req-${req.id}`} className="notifications-list__item">
                      <div className="notifications-list__meta">
                        <span className={`status-pill ${REQUEST_STATUS_TONE[req.status] || "info"}`}>
                          {req.status}
                        </span>
                        <div className="notifications-list__meta-right">
                          <time className="notifications-list__time" dateTime={req.decidedAt || req.createdAt}>
                            {formatAlertTime(req.decidedAt || req.createdAt)}
                          </time>
                          <button
                            type="button"
                            className="notifications-list__dismiss"
                            onClick={() => onDismissRequestUpdate?.(req)}
                            aria-label="Dismiss notification"
                          >
                            <svg width="12" height="12" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" /></svg>
                          </button>
                        </div>
                      </div>
                      <p className="notifications-list__title">
                        {req.clusterName || req.clusterId} request {req.status}
                      </p>
                      <p className="muted notifications-list__detail">
                        {req.decidedByName ? `By ${req.decidedByName}` : ""}
                        {req.decidedByName && req.message ? " · " : ""}
                        {req.message || ""}
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {canViewAlerts ? (
              <div className="notifications-section">
                {hasRequestItems ? <p className="notifications-dropdown__section-label">Alerts</p> : null}
                {!notificationsEnabled ? (
                  <p className="muted notifications-dropdown__empty">Alert notifications are turned off in Settings.</p>
                ) : firingAlerts.length === 0 ? (
                  hasRequestItems ? null : (
                    <p className="muted notifications-dropdown__empty">No firing alerts for this cluster.</p>
                  )
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
            ) : null}

            {!canViewAlerts && canViewRequests && !hasRequestItems ? (
              <p className="muted notifications-dropdown__empty">No request updates yet.</p>
            ) : null}
          </>
        )}
      </div>

      {canViewAlerts || canViewRequests ? (
        <div className="notifications-dropdown__footer notifications-dropdown__footer--actions">
          {canViewRequests ? (
            <button type="button" className="btn-outline notifications-dropdown__view-all" onClick={onViewAllRequests}>
              My Requests
            </button>
          ) : null}
          {canViewAlerts ? (
            <button type="button" className="btn-outline notifications-dropdown__view-all" onClick={onViewAllAlerts}>
              View all alerts
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
