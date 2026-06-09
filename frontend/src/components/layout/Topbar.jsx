import { useEffect, useRef, useState } from "react";
import NotificationsDropdown from "./NotificationsDropdown.jsx";

function formatBadgeCount(count) {
  if (count <= 0) {
    return null;
  }
  return count > 9 ? "9+" : String(count);
}

export default function Topbar({
  allowedClusters,
  allowedNamespaces,
  selectedClusterId,
  selectedNamespace,
  onClusterChange,
  onNamespaceChange,
  showClusterSelector = true,
  showNamespaceSelector = true,
  loadingCore,
  loadingNamespaces = false,
  loadingResources = false,
  loadingPage,
  alertBadgeCount,
  notifications = [],
  clusterLabel = "",
  canViewAlerts = false,
  notificationsEnabled = true,
  onViewAllAlerts,
  displayUser,
  userInitials,
  onLogout,
}) {
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const notificationsRef = useRef(null);
  const badgeLabel = formatBadgeCount(alertBadgeCount);

  useEffect(() => {
    setNotificationsOpen(false);
  }, [selectedClusterId]);

  useEffect(() => {
    if (!notificationsOpen) {
      return undefined;
    }

    const handlePointerDown = (event) => {
      if (notificationsRef.current && !notificationsRef.current.contains(event.target)) {
        setNotificationsOpen(false);
      }
    };

    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        setNotificationsOpen(false);
      }
    };

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [notificationsOpen]);

  const toggleNotifications = () => {
    setNotificationsOpen((open) => !open);
  };

  const handleViewAllAlerts = () => {
    setNotificationsOpen(false);
    onViewAllAlerts?.();
  };

  return (
    <header className="topbar">
      <div className="topbar-selectors">
        {showClusterSelector ? (
          <div className="topbar-field">
            <p className="eyebrow">Active Cluster</p>
            <select
              value={selectedClusterId}
              onChange={(event) => onClusterChange(event.target.value)}
              disabled={loadingCore || !allowedClusters.length}
            >
              {allowedClusters.map((cluster) => (
                <option key={cluster.id} value={cluster.id}>
                  {cluster.name}
                </option>
              ))}
              {!allowedClusters.length ? <option value="">No clusters assigned</option> : null}
            </select>
          </div>
        ) : null}
        {showNamespaceSelector ? (
          <div className="topbar-field">
            <p className="eyebrow">Active Namespace</p>
            <select
              value={selectedNamespace}
              onChange={(event) => onNamespaceChange(event.target.value)}
              disabled={loadingNamespaces || loadingResources || !allowedNamespaces.length}
            >
              {allowedNamespaces.map((namespace) => (
                <option key={namespace.name} value={namespace.name}>
                  {namespace.name}
                </option>
              ))}
              {!allowedNamespaces.length ? <option value="">No namespaces assigned</option> : null}
            </select>
          </div>
        ) : null}
      </div>
      <div className="topbar-actions">
        <div className="notifications-anchor" ref={notificationsRef}>
          <button
            type="button"
            className={`icon-button btn-ghost${notificationsOpen ? " is-active" : ""}`}
            aria-label="Notifications"
            aria-expanded={notificationsOpen}
            aria-haspopup="dialog"
            onClick={toggleNotifications}
          >
            <span aria-hidden="true">🔔</span>
            {badgeLabel ? <span className="notification-badge">{badgeLabel}</span> : null}
          </button>
          <NotificationsDropdown
            open={notificationsOpen}
            alerts={notifications}
            clusterLabel={clusterLabel}
            canViewAlerts={canViewAlerts}
            notificationsEnabled={notificationsEnabled}
            onViewAllAlerts={handleViewAllAlerts}
            onClose={() => setNotificationsOpen(false)}
          />
        </div>
        <button type="button" className="icon-button btn-ghost" aria-label="Help">
          <span aria-hidden="true">?</span>
        </button>
        <button type="button" className="btn-outline" onClick={onLogout}>
          Logout
        </button>
        <div className="topbar-user">
          <div className="user-avatar">{userInitials || "U"}</div>
          <div className="user-meta">
            <strong>{displayUser.name}</strong>
            <span>{displayUser.role}</span>
          </div>
        </div>
      </div>
    </header>
  );
}
