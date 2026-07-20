import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import ConfirmActionModal from "../components/inventory/ConfirmActionModal.jsx";
import AppDrawer from "../components/mobileApps/AppDrawer.jsx";
import {
  ACTIVE_BUILD_STATUSES,
  ACTIVE_PUBLISH_STATUSES,
  AppAvatar,
  BuildStatusPill,
  IconChevron,
  IconGear,
  IconPackage,
  IconRocket,
  IconTicketFile,
  PlatformBadge,
  StoreReadinessRows,
  timeAgo,
} from "../components/mobileApps/common.jsx";
import {
  createMobileApp,
  deleteMobileApp,
  deleteMobileBuild,
  downloadBuild,
  fetchMobileAppBuilds,
  listMobileAppBuilds,
  listMobileAppEnvironments,
  listMobileAppPublishes,
  listMobileApps,
  publishMobileBuild,
  testMobileAppAppStore,
  testMobileAppJenkins,
  testMobileAppPlay,
  updateMobileApp,
  uploadMobileBuild,
} from "../api/mobileAppsApi.js";

const AppFormModal = lazy(() => import("../components/mobileApps/AppFormModal.jsx"));
const PublishDialog = lazy(() => import("../components/mobileApps/PublishDialog.jsx"));
const UploadBuildModal = lazy(() => import("../components/mobileApps/UploadBuildModal.jsx"));

const POLL_INTERVAL_MS = 8000;

// One operational state per app, derived from its latest build — drives both
// the KPI tiles (which double as filters) and each card's build strip.
function appState(app) {
  const s = app?.latestBuild?.status;
  if (s === "available") return "ready";
  if (ACTIVE_BUILD_STATUSES.has(s)) return "inflight";
  if (s === "failed") return "failed";
  return "none";
}

export default function MobileAppsPage({ canManage = false, canPublish = false }) {
  // Tile filter: "all" | "ready" | "inflight" | "failed".
  const [filter, setFilter] = useState("all");
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
  // Custom Zoho environment names for the form's dropdown; null = not loaded
  // (or fetch failed), which makes the modal fall back to a free-text input.
  const [envOptions, setEnvOptions] = useState(null);

  // Upload-binary modal.
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");

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

  const runUpload = async ({ file, platform, version }) => {
    if (!selectedApp) return;
    setUploading(true);
    setUploadError("");
    try {
      const build = await uploadMobileBuild(selectedApp.id, { file, platform, version });
      setUploadOpen(false);
      setNotice(
        `Uploaded ${build?.fileName || "binary"} to ${selectedApp.name}. Ready to publish.`
      );
      await Promise.all([loadDrawerData(selectedApp.id), refreshApps()]);
    } catch (err) {
      setUploadError(err.message || "Failed to upload the binary.");
    } finally {
      setUploading(false);
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

  // Refresh the environment dropdown each time the form opens so it reflects
  // custom environments added in the Zoho settings since the page loaded.
  useEffect(() => {
    if (!formOpen) return;
    let cancelled = false;
    listMobileAppEnvironments()
      .then((res) => {
        if (!cancelled) setEnvOptions(Array.isArray(res?.items) ? res.items : []);
      })
      .catch(() => {
        if (!cancelled) setEnvOptions(null);
      });
    return () => {
      cancelled = true;
    };
  }, [formOpen]);

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
    let buildsHeld = 0;
    let publishes = 0;
    const envs = new Set();
    apps.forEach((a) => {
      if (a.enabled) enabled += 1;
      if (a.zohoEnvironment) envs.add(a.zohoEnvironment.trim().toLowerCase());
      buildsHeld += a.buildCount || 0;
      publishes += a.publishCount || 0;
      const s = appState(a);
      if (s === "ready") ready += 1;
      else if (s === "inflight") inFlight += 1;
      else if (s === "failed") failed += 1;
    });
    return {
      count: apps.length,
      enabled,
      ready,
      inFlight,
      failed,
      envs: envs.size,
      buildsHeld,
      publishes,
    };
  }, [apps]);

  const visibleApps = useMemo(
    () => (filter === "all" ? apps : apps.filter((a) => appState(a) === filter)),
    [apps, filter]
  );

  const tiles = [
    { key: "all", label: "Applications", value: stats.count, detail: `${stats.enabled} enabled` },
    { key: "ready", label: "Ready", value: stats.ready, tone: "ok", detail: "latest build downloadable" },
    { key: "inflight", label: "In flight", value: stats.inFlight, tone: "warn", detail: "fetching or publishing" },
    { key: "failed", label: "Failed", value: stats.failed, tone: "danger", detail: "latest build failed" },
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

      {/* ── Flow strip: the feature drawn as its pipeline ──────────── */}
      <div className="sg-ma-flow" role="group" aria-label="Release pipeline">
        <div className="sg-ma-fnode">
          <span className="sg-ma-fico">
            <IconTicketFile />
          </span>
          <span className="sg-ma-fbody">
            <span className="sg-ma-fk">Zoho ticket</span>
            <span className="sg-ma-fv">
              {stats.envs} environment{stats.envs === 1 ? "" : "s"} <small>linked</small>
            </span>
          </span>
        </div>
        <span className="sg-ma-flink" aria-hidden="true">
          <IconChevron />
        </span>
        <div className="sg-ma-fnode">
          <span className={`sg-ma-fico${stats.inFlight ? " sg-ma-fico--hot" : ""}`}>
            <IconGear />
          </span>
          <span className="sg-ma-fbody">
            <span className="sg-ma-fk">Jenkins build</span>
            <span className="sg-ma-fv">
              {stats.inFlight ? (
                <>
                  {stats.inFlight} fetching <small>now</small>
                </>
              ) : (
                <>
                  idle <small>pulls on ticket success</small>
                </>
              )}
            </span>
          </span>
        </div>
        <span className="sg-ma-flink" aria-hidden="true">
          <IconChevron />
        </span>
        <div className="sg-ma-fnode">
          <span className="sg-ma-fico">
            <IconPackage />
          </span>
          <span className="sg-ma-fbody">
            <span className="sg-ma-fk">Binary store</span>
            <span className="sg-ma-fv">
              {stats.buildsHeld} build{stats.buildsHeld === 1 ? "" : "s"} <small>held</small>
            </span>
          </span>
        </div>
        <span className="sg-ma-flink" aria-hidden="true">
          <IconChevron />
        </span>
        <div className="sg-ma-fnode">
          <span className="sg-ma-fico">
            <IconRocket />
          </span>
          <span className="sg-ma-fbody">
            <span className="sg-ma-fk">Store publish</span>
            <span className="sg-ma-fv">
              {stats.publishes} publish{stats.publishes === 1 ? "" : "es"}{" "}
              <small>Google Play · App Store</small>
            </span>
          </span>
        </div>
      </div>

      {/* ── KPI tiles double as filters ────────────────────────────── */}
      <div className="sg-ma-tiles" role="group" aria-label="Filter applications">
        {tiles.map((t) => {
          const pressed = filter === t.key;
          return (
            <button
              key={t.key}
              type="button"
              className="sg-kpi sg-ma-tile"
              aria-pressed={pressed}
              onClick={() => setFilter(t.key === "all" || pressed ? "all" : t.key)}
              title={
                t.key === "all"
                  ? "Show all applications"
                  : pressed
                    ? "Clear filter"
                    : `Show only ${t.label.toLowerCase()} applications`
              }
            >
              <p className="sg-kpi-label">{t.label}</p>
              <span className="sg-ma-tile-value">
                {t.value}
                {t.tone && t.value > 0 ? (
                  <span className={`sg-ma-tile-dot sg-ma-tile-dot--${t.tone}`} aria-hidden="true" />
                ) : null}
              </span>
              <span className="sg-ma-tile-detail">{t.detail}</span>
              {t.key !== "all" ? (
                <span className="sg-ma-tile-flag" aria-hidden="true">
                  filtering
                </span>
              ) : null}
            </button>
          );
        })}
      </div>

      {/* ── App identity cards ─────────────────────────────────────── */}
      {loading ? (
        <p className="muted">Loading applications…</p>
      ) : apps.length ? (
        visibleApps.length ? (
          <div className="sg-ma-grid">
            {visibleApps.map((app) => (
              <button
                key={app.id}
                type="button"
                className={`sg-ma-card${app.enabled ? "" : " sg-ma-card--dim"}`}
                onClick={() => openApp(app)}
                aria-haspopup="dialog"
              >
                <span className="sg-ma-card-top">
                  <AppAvatar name={app.name} enabled={app.enabled} />
                  <span className="sg-ma-card-id">
                    <span className="sg-ma-card-name">
                      {app.name}
                      <span
                        className={`sg-ma-endot${app.enabled ? "" : " sg-ma-endot--off"}`}
                        title={app.enabled ? "Enabled" : "Disabled"}
                      />
                    </span>
                    {app.description ? (
                      <span className="sg-ma-card-desc">{app.description}</span>
                    ) : null}
                  </span>
                </span>
                <span className="sg-ma-card-chips">
                  {(app.platforms || []).map((p) => (
                    <PlatformBadge key={p} platform={p} />
                  ))}
                  {app.zohoEnvironment ? <span className="sg-tag">{app.zohoEnvironment}</span> : null}
                  {!app.enabled ? <span className="sg-tag">Disabled</span> : null}
                </span>
                <span className="sg-ma-card-build">
                  {app.latestBuild ? (
                    <>
                      <span className="sg-ma-card-ver">{app.latestBuild.version || "—"}</span>
                      <BuildStatusPill status={app.latestBuild.status} />
                      <span className="sg-ma-card-when">
                        {timeAgo(app.latestBuild.createdAt)}
                        {app.latestBuild.ticketNumber ? ` · ${app.latestBuild.ticketNumber}` : ""}
                      </span>
                    </>
                  ) : (
                    <span className="muted">No builds yet</span>
                  )}
                </span>
                <StoreReadinessRows app={app} />
                <span className="sg-ma-card-foot">
                  {app.jenkinsJobPath ? (
                    <span className="sg-ma-count sg-ma-card-job">{app.jenkinsJobPath}</span>
                  ) : (
                    <span className="sg-ma-count">No Jenkins job</span>
                  )}
                  <span className="sg-ma-card-open">Open ›</span>
                </span>
              </button>
            ))}
          </div>
        ) : (
          <p className="muted sg-ma-filter-empty">
            No applications match this filter.{" "}
            <button type="button" className="link-button" onClick={() => setFilter("all")}>
              Show all
            </button>
          </p>
        )
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
          onUpload={() => {
            setUploadError("");
            setUploadOpen(true);
          }}
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
            environments={envOptions}
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

      {uploadOpen ? (
        <Suspense fallback={null}>
          <UploadBuildModal
            open={uploadOpen}
            app={selectedApp}
            onClose={() => setUploadOpen(false)}
            onUpload={runUpload}
            uploading={uploading}
            error={uploadError}
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
