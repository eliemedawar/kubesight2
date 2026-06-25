import { useEffect, useRef, useState } from "react";
import NotificationsDropdown from "./NotificationsDropdown.jsx";
import SearchableSelect from "../common/SearchableSelect.jsx";

const IconBell = () => (
  <svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
    <path d="M10 2a6 6 0 00-6 6v3.586l-.707.707A1 1 0 004 14h12a1 1 0 00.707-1.707L16 11.586V8a6 6 0 00-6-6zM10 18a3 3 0 01-3-3h6a3 3 0 01-3 3z" />
  </svg>
);

const IconQuestion = () => (
  <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
    <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-8-3a1 1 0 00-.867.5 1 1 0 11-1.731-1A3 3 0 0113 8a3.001 3.001 0 01-2 2.83V11a1 1 0 11-2 0v-1a1 1 0 011-1 1 1 0 100-2zm0 8a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
  </svg>
);

const IconMenu = () => (
  <svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
    <path fillRule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 10a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
  </svg>
);

const IconClose = () => (
  <svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
    <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
  </svg>
);

const IconLogout = () => (
  <svg width="15" height="15" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
    <path fillRule="evenodd" d="M3 3a1 1 0 00-1 1v12a1 1 0 102 0V4a1 1 0 00-1-1zm10.293 9.293a1 1 0 001.414 1.414l3-3a1 1 0 000-1.414l-3-3a1 1 0 10-1.414 1.414L14.586 9H7a1 1 0 100 2h7.586l-1.293 1.293z" clipRule="evenodd" />
  </svg>
);

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
  requestUpdates = [],
  canViewRequests = false,
  requestBadgeCount = 0,
  onViewAllRequests,
  onNotificationsOpen,
  onDismissRequestUpdate,
  onClearRequestUpdates,
  displayUser,
  userInitials,
  onLogout,
  onMenuToggle = () => {},
  sidebarOpen = false,
}) {
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const notificationsRef = useRef(null);
  const badgeLabel = formatBadgeCount((alertBadgeCount || 0) + (requestBadgeCount || 0));

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
    setNotificationsOpen((open) => {
      const next = !open;
      if (next) {
        onNotificationsOpen?.();
      }
      return next;
    });
  };

  const handleViewAllAlerts = () => {
    setNotificationsOpen(false);
    onViewAllAlerts?.();
  };

  const handleViewAllRequests = () => {
    setNotificationsOpen(false);
    onViewAllRequests?.();
  };

  return (
    <header className="topbar">
      <div className="topbar-leading">
        <button
          type="button"
          className="icon-button topbar-menu-button"
          aria-label={sidebarOpen ? "Close navigation menu" : "Open navigation menu"}
          aria-expanded={sidebarOpen}
          onClick={onMenuToggle}
        >
          {sidebarOpen ? <IconClose /> : <IconMenu />}
        </button>
        <div className="topbar-selectors">
        {showClusterSelector ? (
          <div className="topbar-field">
            <p className="eyebrow">Active Cluster</p>
            <SearchableSelect
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
            </SearchableSelect>
          </div>
        ) : null}
        {showNamespaceSelector ? (
          <div className="topbar-field">
            <p className="eyebrow">Active Namespace</p>
            <SearchableSelect
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
            </SearchableSelect>
          </div>
        ) : null}
        </div>
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
            <IconBell />
            {badgeLabel ? <span className="notification-badge" aria-label={`${badgeLabel} notifications`}>{badgeLabel}</span> : null}
          </button>
          <NotificationsDropdown
            open={notificationsOpen}
            alerts={notifications}
            requestUpdates={requestUpdates}
            canViewRequests={canViewRequests}
            onViewAllRequests={handleViewAllRequests}
            onDismissRequestUpdate={onDismissRequestUpdate}
            onClearRequestUpdates={onClearRequestUpdates}
            clusterLabel={clusterLabel}
            canViewAlerts={canViewAlerts}
            notificationsEnabled={notificationsEnabled}
            onViewAllAlerts={handleViewAllAlerts}
            onClose={() => setNotificationsOpen(false)}
          />
        </div>
        <button type="button" className="icon-button btn-ghost topbar-help-button" aria-label="Help & documentation">
          <IconQuestion />
        </button>
        <button type="button" className="btn-outline topbar-logout" onClick={onLogout} aria-label="Sign out">
          <IconLogout />
          <span>Sign out</span>
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
