import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import StatCard from "../components/common/StatCard.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import ConfirmActionModal from "../components/inventory/ConfirmActionModal.jsx";
import AppDrawer from "../components/mobileApps/AppDrawer.jsx";
import {
  ACTIVE_BUILD_STATUSES,
  ACTIVE_PUBLISH_STATUSES,
  BuildStatusPill,
  IconSmartphone,
  PlatformBadge,
} from "../components/mobileApps/common.jsx";
import {
  createMobileApp,
  deleteMobileApp,
  deleteMobileBuild,
  downloadBuild,
  fetchMobileAppBuilds,
  listMobileAppBuilds,
  listMobileAppPublishes,
  listMobileApps,
  publishMobileBuild,
  testMobileAppAppStore,
  testMobileAppJenkins,
  testMobileAppPlay,
  updateMobileApp,
} from "../api/mobileAppsApi.js";

const AppFormModal = lazy(() => import("../components/mobileApps/AppFormModal.jsx"));
const PublishDialog = lazy(() => import("../components/mobileApps/PublishDialog.jsx"));

const POLL_INTERVAL_MS = 8000;

export default function MobileAppsPage({ canManage = false, canPublish = false }) {
  const [apps, setApps] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Drawer (one open app at a time) + its loaded builds/publishes.
  const [selectedAppId, setSelectedAppId] = useState(null);
  const [builds, setBuilds] = useState([]);
  const [publishes, setPublishes] = useState([]);
  const [drawerLoading, setDrawerLoading] = useState(false);

  // Per-action busy flags.
  const [fetching, setFetching] = useState(false);
  const [testingJenkins, setTestingJenkins] = useState(false);
  const [testingPlay, setTestingPlay] = useState(false);
  const [testingAppStore, setTestingAppStore] = useState(false);
  const [downloadingBuildId, setDownloadingBuildId] = useState(null);
  const [deletingBuildId, setDeletingBuildId] = useState(null);

  // Form modal.
  const [formOpen, setFormOpen] = useState(false);
  const [formMode, setFormMode] = useState("create");
  const [editingAppId, setEditingAppId] = useState(null);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");

  // Publish dialog + delete confirm.
  const [publishBuild, setPublishBuild] = useState(null);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");
  const [deleteApp, setDeleteApp] = useState(null);
  const [deletingApp, setDeletingApp] = useState(false);

  const selectedApp = useMemo(
    () => apps.find((a) => a.id === selectedAppId) || null,
    [apps, selectedAppId]
  );
  const editingApp = useMemo(
    () => (editingAppId ? apps.find((a) => a.id === editingAppId) || null : null),
    [apps, editingAppId]
  );

  const loadApps = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await listMobileApps();
      setApps(res?.items || []);
    } catch (err) {
      setError(err.message || "Failed to load mobile applications.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadApps();
  }, [loadApps]);

  const refreshApps = useCallback(async () => {
    try {
      const res = await listMobileApps();
      setApps(res?.items || []);
    } catch {
      /* best-effort background refresh */
    }
  }, []);

  const loadDrawerData = useCallback(async (appId) => {
    if (!appId) return;
    setDrawerLoading(true);
    try {
      const [b, p] = await Promise.all([
        listMobileAppBuilds(appId).catch(() => ({ items: [] })),
        listMobileAppPublishes(appId).catch(() => ({ items: [] })),
      ]);
      setBuilds(b?.items || []);
      setPublishes(p?.items || []);
    } finally {
      setDrawerLoading(false);
    }
  }, []);

  const openApp = (app) => {
    setSelectedAppId(app.id);
    setBuilds([]);
    setPublishes([]);
    loadDrawerData(app.id);
  };

  const closeDrawer = () => {
    setSelectedAppId(null);
    setBuilds([]);
    setPublishes([]);
  };

  // Poll while the open app has a build fetching/downloading or a publish in
  // flight; reload that app's builds + publishes and the app list. Stops on its
  // own once everything is terminal (mirrors ZohoSyncPage's hasActiveRun poll).
  const hasActiveWork =
    Boolean(selectedAppId) &&
    (builds.some((b) => ACTIVE_BUILD_STATUSES.has(b.status)) ||
      publishes.some((p) => ACTIVE_PUBLISH_STATUSES.has(p.status)));

  useEffect(() => {
    if (!hasActiveWork) return undefined;
    const timer = setInterval(async () => {
      try {
        const [b, p, listRes] = await Promise.all([
          listMobileAppBuilds(selectedAppId),
          listMobileAppPublishes(selectedAppId),
          listMobileApps(),
        ]);
        setBuilds(b?.items || []);
        setPublishes(p?.items || []);
        setApps(listRes?.items || []);
      } catch {
        /* transient — the next tick retries */
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [hasActiveWork, selectedAppId]);

  // ── Actions ────────────────────────────────────────────────────────
  const runFetch = async () => {
    if (!selectedApp) return;
    setFetching(true);
    setError("");
    setNotice("");
    try {
      const res = await fetchMobileAppBuilds(selectedApp.id);
      const count = res?.builds?.length || 0;
      setNotice(
        count
          ? `Fetched ${count} build${count === 1 ? "" : "s"} for ${selectedApp.name}.`
          : `Fetch started for ${selectedApp.name}.`
      );
      await Promise.all([loadDrawerData(selectedApp.id), refreshApps()]);
    } catch (err) {
      if (err.status === 409) {
        setNotice("The latest build has already been ingested.");
        await Promise.all([loadDrawerData(selectedApp.id), refreshApps()]);
      } else {
        setError(err.message || "Failed to fetch the latest build.");
      }
    } finally {
      setFetching(false);
    }
  };

  const runTestJenkins = async () => {
    if (!selectedApp) return;
    setTestingJenkins(true);
    setError("");
    setNotice("");
    try {
      const result = await testMobileAppJenkins(selectedApp.id);
      if (result.status === "ok") {
        setNotice(result.message || "Jenkins job resolved successfully.");
      } else {
        setError(result.message || "Jenkins test failed.");
      }
      await refreshApps();
    } catch (err) {
      setError(err.message || "Jenkins test failed.");
    } finally {
      setTestingJenkins(false);
    }
  };

  const runTestPlay = async () => {
    if (!editingApp) return;
    setTestingPlay(true);
    setNotice("");
    setError("");
    try {
      const result = await testMobileAppPlay(editingApp.id);
      if (result.status === "ok") {
        setNotice(result.message || "Google Play credentials verified.");
      } else {
        setError(result.message || "Google Play test failed.");
      }
      await refreshApps();
    } catch (err) {
      setError(err.message || "Google Play test failed.");
    } finally {
      setTestingPlay(false);
    }
  };

  const runTestAppStore = async () => {
    if (!editingApp) return;
    setTestingAppStore(true);
    setNotice("");
    setError("");
    try {
      const result = await testMobileAppAppStore(editingApp.id);
      if (result.status === "ok") {
        setNotice(result.message || "App Store Connect credentials verified.");
      } else {
        setError(result.message || "App Store Connect test failed.");
      }
      await refreshApps();
    } catch (err) {
      setError(err.message || "App Store Connect test failed.");
    } finally {
      setTestingAppStore(false);
    }
  };

  const openCreate = () => {
    setFormMode("create");
    setEditingAppId(null);
    setFormError("");
    setFormOpen(true);
  };

  const openEdit = () => {
    if (!selectedApp) return;
    setFormMode("edit");
    setEditingAppId(selectedApp.id);
    setFormError("");
    setFormOpen(true);
  };

  const saveApp = async (payload) => {
    setSaving(true);
    setFormError("");
    setNotice("");
    try {
      if (formMode === "edit" && editingAppId) {
        await updateMobileApp(editingAppId, payload);
        setNotice(`Saved changes to ${payload.name}.`);
      } else {
        await createMobileApp(payload);
        setNotice(`Registered ${payload.name}.`);
      }
      setFormOpen(false);
      await refreshApps();
      if (formMode === "edit" && editingAppId === selectedAppId) {
        await loadDrawerData(selectedAppId);
      }
    } catch (err) {
      setFormError(err.message || "Failed to save the application.");
    } finally {
      setSaving(false);
    }
  };

  const confirmDeleteApp = async () => {
    if (!deleteApp) return;
    setDeletingApp(true);
    setError("");
    try {
      await deleteMobileApp(deleteApp.id);
      setNotice(`Deleted ${deleteApp.name}.`);
      if (selectedAppId === deleteApp.id) closeDrawer();
      setDeleteApp(null);
      await refreshApps();
    } catch (err) {
      setError(err.message || "Failed to delete the application.");
    } finally {
      setDeletingApp(false);
    }
  };

  const runDownload = async (build) => {
    setDownloadingBuildId(build.id);
    setError("");
    try {
      await downloadBuild(build.id, build.fileName);
    } catch (err) {
      setError(err.message || "Failed to download the build.");
    } finally {
      setDownloadingBuildId(null);
    }
  };

  const runDeleteBuild = async (build) => {
    if (!window.confirm(`Delete build ${build.version || build.fileName || `#${build.id}`}?`)) {
      return;
    }
    setDeletingBuildId(build.id);
    setError("");
    try {
      await deleteMobileBuild(build.id);
      setBuilds((prev) => prev.filter((b) => b.id !== build.id));
      refreshApps();
    } catch (err) {
      setError(err.message || "Failed to delete the build.");
    } finally {
      setDeletingBuildId(null);
    }
  };

  const runPublish = async ({ store, target }) => {
    if (!publishBuild) return;
    setPublishing(true);
    setPublishError("");
    try {
      await publishMobileBuild(publishBuild.id, { store, target });
      setNotice(`Publishing ${selectedApp?.name || "build"} to ${target}.`);
      setPublishBuild(null);
      if (selectedAppId) await loadDrawerData(selectedAppId);
    } catch (err) {
      setPublishError(err.message || "Failed to start publishing.");
    } finally {
      setPublishing(false);
    }
  };

  // ── Stats (fleet-level, from each app's latest build) ───────────────
  const stats = useMemo(() => {
    let ready = 0;
    let inFlight = 0;
    let failed = 0;
    let enabled = 0;
    apps.forEach((a) => {
      if (a.enabled) enabled += 1;
      const s = a.latestBuild?.status;
      if (s === "available") ready += 1;
      else if (ACTIVE_BUILD_STATUSES.has(s)) inFlight += 1;
      else if (s === "failed") failed += 1;
    });
    return { count: apps.length, enabled, ready, inFlight, failed };
  }, [apps]);

  const columns = [
    "Name",
    "Platforms",
    "Zoho environment",
    "Jenkins job",
    "Latest build",
    "Store credentials",
  ];

  return (
    <div className="sg-ma-page">
      <PageTitle
        title="Mobile Applications"
        subtitle="Pull Jenkins-built APK, AAB and IPA binaries from deployment tickets and publish them to Google Play and the App Store."
        actionLabel={canManage ? "Register application" : undefined}
        onAction={canManage ? openCreate : undefined}
      />

      {error ? <ErrorBanner message={error} /> : null}
      {notice ? (
        <div className="banner banner-success" role="status">
          <span>{notice}</span>
          <button type="button" className="link-button" onClick={() => setNotice("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      <section className="stat-grid sg-ma-stats">
        <StatCard
          title="Applications"
          value={stats.count}
          detail={`${stats.enabled} enabled`}
          icon={<IconSmartphone width={20} height={20} />}
        />
        <StatCard
          title="Builds ready"
          value={stats.ready}
          detail="latest build downloadable"
          tone={stats.ready ? "ok" : "default"}
        />
        <StatCard
          title="In flight"
          value={stats.inFlight}
          detail="fetching or publishing"
          tone={stats.inFlight ? "warn" : "default"}
        />
        <StatCard
          title="Failed"
          value={stats.failed}
          detail="latest build failed"
          tone={stats.failed ? "danger" : "default"}
        />
      </section>

      {loading ? (
        <p className="muted">Loading applications…</p>
      ) : apps.length ? (
        <div className="table-shell sg-ma-table-shell" role="region" aria-label="Mobile applications" tabIndex={0}>
          <table className="sg-ma-table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {apps.map((app) => (
                <tr
                  key={app.id}
                  className="data-table-row--clickable"
                  onClick={() => openApp(app)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      openApp(app);
                    }
                  }}
                  tabIndex={0}
                  role="button"
                >
                  <td>
                    <div className="sg-ma-cell-name">
                      <b>{app.name}</b>
                      {app.description ? (
                        <span className="sg-ma-cell-desc">{app.description}</span>
                      ) : null}
                    </div>
                  </td>
                  <td>
                    <span className="sg-ma-plats">
                      {(app.platforms || []).length ? (
                        app.platforms.map((p) => <PlatformBadge key={p} platform={p} />)
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </span>
                  </td>
                  <td>
                    {app.zohoEnvironment ? (
                      <span className="sg-tag">{app.zohoEnvironment}</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="mono sg-ma-wrap">{app.jenkinsJobPath || "—"}</td>
                  <td>
                    {app.latestBuild ? (
                      <span className="sg-ma-latest">
                        <span className="mono">{app.latestBuild.version || "—"}</span>
                        <BuildStatusPill status={app.latestBuild.status} />
                      </span>
                    ) : (
                      <span className="muted">No builds</span>
                    )}
                  </td>
                  <td>
                    <span className="sg-ma-creds">
                      <span className={`status-pill ${app.playServiceAccountConfigured ? "ok" : "muted"}`}>
                        Play
                      </span>
                      <span className={`status-pill ${app.ascPrivateKeyConfigured ? "ok" : "muted"}`}>
                        ASC
                      </span>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState
          variant="applications"
          message="No mobile applications yet"
          hint={
            canManage
              ? "Register an application to link its Jenkins job and store credentials, then fetch builds from deployment tickets."
              : "No applications have been registered yet. Ask an administrator to add one."
          }
        />
      )}

      {selectedApp ? (
        <AppDrawer
          app={selectedApp}
          canManage={canManage}
          canPublish={canPublish}
          onClose={closeDrawer}
          builds={builds}
          publishes={publishes}
          loading={drawerLoading}
          onFetch={runFetch}
          fetching={fetching}
          onTestJenkins={runTestJenkins}
          testingJenkins={testingJenkins}
          onEdit={openEdit}
          onDelete={() => setDeleteApp(selectedApp)}
          onDownload={runDownload}
          downloadingBuildId={downloadingBuildId}
          onDeleteBuild={runDeleteBuild}
          deletingBuildId={deletingBuildId}
          onPublishBuild={(build) => {
            setPublishError("");
            setPublishBuild(build);
          }}
        />
      ) : null}

      {formOpen ? (
        <Suspense fallback={null}>
          <AppFormModal
            open={formOpen}
            mode={formMode}
            app={editingApp}
            onClose={() => setFormOpen(false)}
            onSave={saveApp}
            saving={saving}
            error={formError}
            onTestPlay={runTestPlay}
            onTestAppStore={runTestAppStore}
            testingPlay={testingPlay}
            testingAppStore={testingAppStore}
          />
        </Suspense>
      ) : null}

      {publishBuild ? (
        <Suspense fallback={null}>
          <PublishDialog
            open={Boolean(publishBuild)}
            app={selectedApp}
            build={publishBuild}
            onClose={() => setPublishBuild(null)}
            onConfirm={runPublish}
            publishing={publishing}
            error={publishError}
          />
        </Suspense>
      ) : null}

      <ConfirmActionModal
        open={Boolean(deleteApp)}
        title="Delete application"
        message={
          deleteApp
            ? `Delete “${deleteApp.name}” and its build/publish history from KubeSight? This does not remove anything already published to the stores.`
            : ""
        }
        confirmLabel="Delete application"
        danger
        busy={deletingApp}
        onClose={() => setDeleteApp(null)}
        onConfirm={confirmDeleteApp}
      />
    </div>
  );
}
