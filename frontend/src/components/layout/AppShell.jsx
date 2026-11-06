import { useCallback, useEffect, useMemo, useState } from "react";
import ErrorBanner from "../common/ErrorBanner";
import { useEscapeToClose } from "../../hooks/useEscapeToClose.js";
import LoadingState from "../common/LoadingState";
import { buildJumpTargets, buildNavGroups, workspaceOfPage } from "../../lib/navigation.js";
import DialogHost from "../common/DialogHost.jsx";
import AssistantPanel from "../assistant/AssistantPanel.jsx";
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
  assistantEnabled = false,
  children,
}) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [jumpOpen, setJumpOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);

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

  // Ctrl/⌘+K opens the jump palette from anywhere; Ctrl/⌘+G the Hermes chat
  // (admins only — the backend refuses everyone else too).
  useEffect(() => {
    const handleKeyDown = (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        setJumpOpen((isOpen) => !isOpen);
      } else if (key === "g" && assistantEnabled) {
        // Also the browser's "find next" — taken over only for admins.
        event.preventDefault();
        setAssistantOpen((isOpen) => !isOpen);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [assistantEnabled]);

  useEffect(() => {
    if (!assistantEnabled) setAssistantOpen(false);
  }, [assistantEnabled]);

  // What Hermes is told is on screen, so "this pod" means the one shown here.
  const pageLabel = useMemo(() => {
    for (const group of navGroups) {
      for (const item of group.items) {
        if (item.key === activePage) return item.label;
        const tab = item.tabs?.find((t) => t.pageKey === activePage);
        if (tab) return `${item.label} › ${tab.label}`;
      }
    }
    return activePage || "";
  }, [navGroups, activePage]);
  const assistantContext = useMemo(
    () => ({
      page: activePage || "",
      pageLabel,
      clusterId: showClusterSelector && selectedClusterId ? String(selectedClusterId) : "",
      clusterName: showClusterSelector ? clusterLabel || "" : "",
      namespace: showNamespaceSelector ? selectedNamespace || "" : "",
      route: assistantOpen ? window.location.hash : "",
    }),
    [activePage, pageLabel, showClusterSelector, selectedClusterId, clusterLabel, showNamespaceSelector, selectedNamespace, assistantOpen]
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
      {assistantEnabled ? (
        <AssistantPanel
          open={assistantOpen}
          onClose={() => setAssistantOpen(false)}
          context={assistantContext}
        />
      ) : null}
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
