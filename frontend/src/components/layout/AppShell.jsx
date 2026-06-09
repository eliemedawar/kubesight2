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
  displayUser,
  userInitials,
  onLogout,
  children,
}) {
  return (
    <div className="shell">
      <Sidebar pages={visiblePages} activePage={activePage} onNavigate={onNavigate} />
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
          displayUser={displayUser}
          userInitials={userInitials}
          onLogout={onLogout}
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
