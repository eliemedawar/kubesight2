import { useCallback, useEffect, useMemo, useState } from "react";
import ErrorBanner from "../common/ErrorBanner";
import { useEscapeToClose } from "../../hooks/useEscapeToClose.js";
import LoadingState from "../common/LoadingState";
import { buildJumpTargets, buildNavGroups, workspaceOfPage } from "../../lib/navigation.js";
import DialogHost from "../common/DialogHost.jsx";
import JumpPalette from "./JumpPalette";
import Sidebar from "./Sidebar";
import Topbar from "./Topbar";
import WorkspaceTabs from "./WorkspaceTabs";

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
  onStartTour,
  children,
}) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [jumpOpen, setJumpOpen] = useState(false);

  const navGroups = useMemo(() => buildNavGroups(visiblePages), [visiblePages]);
  const jumpTargets = useMemo(() => buildJumpTargets(navGroups), [navGroups]);
  const workspaceKey = workspaceOfPage(activePage);
  const workspaceTabs = workspaceKey
    ? navGroups.flatMap((group) => group.items).find((item) => item.key === workspaceKey)?.tabs
    : null;

  const closeSidebar = useCallback(() => setSidebarOpen(false), []);
  const toggleSidebar = useCallback(() => setSidebarOpen((open) => !open), []);

  const handleNavigate = useCallback(
    (pageKey) => {
      onNavigate(pageKey);
      closeSidebar();
    },
    [closeSidebar, onNavigate]
  );

  // Ctrl/⌘+K opens the jump palette from anywhere.
  useEffect(() => {
    const handleKeyDown = (event) => {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setJumpOpen((isOpen) => !isOpen);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

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

  useEscapeToClose(closeSidebar, true, sidebarOpen);

  return (
    <div className={`shell${sidebarOpen ? " shell--sidebar-open" : ""}`}>
      <button
        type="button"
        className="sidebar-backdrop"
        aria-label="Close navigation"
        onClick={closeSidebar}
        tabIndex={sidebarOpen ? 0 : -1}
      />
      <Sidebar
        pages={visiblePages}
        activePage={activePage}
        onNavigate={handleNavigate}
        onOpenJump={() => setJumpOpen(true)}
        open={sidebarOpen}
      />
      <DialogHost />
      <JumpPalette
        open={jumpOpen}
        targets={jumpTargets}
        onClose={() => setJumpOpen(false)}
        onSelect={(target) => {
          setJumpOpen(false);
          handleNavigate(target.pageKey);
        }}
      />
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
          onStartTour={onStartTour}
          sidebarOpen={sidebarOpen}
        />
        {loadingOverlay ? (
          <LoadingState label={loadingOverlayLabel} hint={loadingOverlayHint} />
        ) : null}
        <ErrorBanner message={errorMessage} />
        {/* "No clusters assigned" is a heads-up, not a success: warning tone. */}
        {clusterBannerMessage ? (
          <p className="banner-message warning-banner" role="status">{clusterBannerMessage}</p>
        ) : null}
        <main className="page-content ops-layout">
          {workspaceTabs ? (
            <WorkspaceTabs
              workspaceKey={workspaceKey}
              tabs={workspaceTabs}
              activePageKey={activePage}
              onNavigate={handleNavigate}
            />
          ) : null}
          {children}
        </main>
      </section>
    </div>
  );
}
