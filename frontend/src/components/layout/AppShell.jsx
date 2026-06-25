import { useCallback, useEffect, useState } from "react";
import ErrorBanner from "../common/ErrorBanner";
import LoadingState from "../common/LoadingState";
import Sidebar from "./Sidebar";
import Topbar from "./Topbar";

export default function AppShell({
  visiblePages,
  activePage,
  onNavigate,
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
  loadingOverlay,
  loadingOverlayLabel,
  loadingOverlayHint,
  errorMessage,
  clusterBannerMessage,
  alertBadgeCount,
  notifications,
  clusterLabel,
  canViewAlerts,
  notificationsEnabled,
  onViewAllAlerts,
  requestUpdates,
  canViewRequests,
  requestBadgeCount,
  onViewAllRequests,
  onNotificationsOpen,
  onDismissRequestUpdate,
  onClearRequestUpdates,
  displayUser,
  userInitials,
  onLogout,
  children,
}) {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const closeSidebar = useCallback(() => setSidebarOpen(false), []);
  const toggleSidebar = useCallback(() => setSidebarOpen((open) => !open), []);

  const handleNavigate = useCallback(
    (pageKey) => {
      onNavigate(pageKey);
      closeSidebar();
    },
    [closeSidebar, onNavigate]
  );

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const handleChange = () => {
      if (media.matches) {
        closeSidebar();
      }
    };
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, [closeSidebar]);

  useEffect(() => {
    document.body.classList.toggle("sidebar-drawer-open", sidebarOpen);
    return () => document.body.classList.remove("sidebar-drawer-open");
  }, [sidebarOpen]);

  useEffect(() => {
    if (!sidebarOpen) {
      return undefined;
    }
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        closeSidebar();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [closeSidebar, sidebarOpen]);

  return (
    <div className={`shell${sidebarOpen ? " shell--sidebar-open" : ""}`}>
      <button
        type="button"
        className="sidebar-backdrop"
        aria-label="Close navigation"
        onClick={closeSidebar}
        tabIndex={sidebarOpen ? 0 : -1}
      />
      <Sidebar pages={visiblePages} activePage={activePage} onNavigate={handleNavigate} open={sidebarOpen} />
      <section className="workspace">
        <Topbar
          allowedClusters={allowedClusters}
          allowedNamespaces={allowedNamespaces}
          selectedClusterId={selectedClusterId}
          selectedNamespace={selectedNamespace}
          onClusterChange={onClusterChange}
          onNamespaceChange={onNamespaceChange}
          showClusterSelector={showClusterSelector}
          showNamespaceSelector={showNamespaceSelector}
          loadingCore={loadingCore}
          loadingNamespaces={loadingNamespaces}
          loadingResources={loadingResources}
          loadingPage={loadingPage}
          alertBadgeCount={alertBadgeCount}
          notifications={notifications}
          clusterLabel={clusterLabel}
          canViewAlerts={canViewAlerts}
          notificationsEnabled={notificationsEnabled}
          onViewAllAlerts={onViewAllAlerts}
          requestUpdates={requestUpdates}
          canViewRequests={canViewRequests}
          requestBadgeCount={requestBadgeCount}
          onViewAllRequests={onViewAllRequests}
          onNotificationsOpen={onNotificationsOpen}
          onDismissRequestUpdate={onDismissRequestUpdate}
          onClearRequestUpdates={onClearRequestUpdates}
          displayUser={displayUser}
          userInitials={userInitials}
          onLogout={onLogout}
          onMenuToggle={toggleSidebar}
          sidebarOpen={sidebarOpen}
        />
        {loadingOverlay ? (
          <LoadingState label={loadingOverlayLabel} hint={loadingOverlayHint} />
        ) : null}
        <ErrorBanner message={errorMessage} />
        {clusterBannerMessage ? <p className="banner-message">{clusterBannerMessage}</p> : null}
        <main className="page-content ops-layout">{children}</main>
      </section>
    </div>
  );
}
