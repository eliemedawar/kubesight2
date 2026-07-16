import {
  ACTIVE_BUILD_STATUSES,
  BuildStatusPill,
  formatBytes,
  IconClose,
  IconDownload,
  IconExternal,
  IconRocket,
  IconTrash,
  PlatformBadge,
  PublishStatusPill,
  PublishSteps,
  storeTargetLabel,
  TestStatusPill,
  timeAgo,
} from "./common.jsx";

function CredPill({ label, configured }) {
  return (
    <span className={`status-pill ${configured ? "ok" : "muted"}`}>
      {label}: {configured ? "configured" : "missing"}
    </span>
  );
}

function BuildRow({
  build,
  canManage,
  canPublish,
  onDownload,
  downloading,
  onDeleteBuild,
  deleting,
  onPublishBuild,
}) {
  const available = build.status === "available";
  return (
    <li className="sg-ma-build">
      <div className="sg-ma-build-main">
        <div className="sg-ma-build-head">
          <PlatformBadge platform={build.platform} />
          <b className="sg-ma-build-ver">{build.version || "—"}</b>
          <span className="sg-tag">{(build.artifactType || "").toUpperCase()}</span>
          <BuildStatusPill status={build.status} />
          {build.source === "manual" ? <span className="sg-ma-count">manual</span> : null}
        </div>
        <div className="sg-ma-build-meta">
          <span className="sg-ma-build-file" title={build.fileName}>
            {build.fileName || "—"}
          </span>
          <span className="sg-ma-dot" aria-hidden="true">
            ·
          </span>
          <span>{formatBytes(build.fileSize)}</span>
          {build.ticketNumber ? (
            <>
              <span className="sg-ma-dot" aria-hidden="true">
                ·
              </span>
              <span className="sg-tag">{build.ticketNumber}</span>
            </>
          ) : null}
          {build.jenkinsBuildNumber ? (
            <>
              <span className="sg-ma-dot" aria-hidden="true">
                ·
              </span>
              {build.jenkinsBuildUrl ? (
                <a
                  href={build.jenkinsBuildUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="sg-ma-jlink"
                >
                  Jenkins #{build.jenkinsBuildNumber}
                  <IconExternal width={12} height={12} />
                </a>
              ) : (
                <span>Jenkins #{build.jenkinsBuildNumber}</span>
              )}
            </>
          ) : null}
        </div>
        {build.status === "failed" && build.error ? (
          <p className="sg-ma-inline-error">{build.error}</p>
        ) : null}
      </div>
      <div className="sg-ma-build-actions">
        {available ? (
          <button
            type="button"
            className="icon-button"
            onClick={() => onDownload(build)}
            disabled={downloading}
            title="Download artifact"
            aria-label="Download artifact"
          >
            <IconDownload />
          </button>
        ) : null}
        {canPublish && available ? (
          <button type="button" className="btn-outline sg-ma-pubbtn" onClick={() => onPublishBuild(build)}>
            <IconRocket width={14} height={14} />
            Publish…
          </button>
        ) : null}
        {canManage ? (
          <button
            type="button"
            className="icon-button sg-ma-danger-icon"
            onClick={() => onDeleteBuild(build)}
            disabled={deleting}
            title="Delete build"
            aria-label="Delete build"
          >
            <IconTrash />
          </button>
        ) : null}
      </div>
    </li>
  );
}

function PublishCard({ publish }) {
  return (
    <li className="sg-ma-pub">
      <div className="sg-ma-pub-head">
        <b>{storeTargetLabel(publish.store, publish.target)}</b>
        <PublishStatusPill status={publish.status} />
      </div>
      <PublishSteps steps={publish.steps} />
      {publish.storeRef ? (
        <p className="sg-ma-pub-ref mono">{publish.storeRef}</p>
      ) : null}
      {publish.status === "failed" && publish.error ? (
        <p className="sg-ma-inline-error">{publish.error}</p>
      ) : null}
      <p className="sg-ma-pub-foot">
        {publish.triggeredBy ? <span>by {publish.triggeredBy}</span> : null}
        <span className="sg-ma-htime">{timeAgo(publish.finishedAt || publish.createdAt)}</span>
      </p>
    </li>
  );
}

export default function AppDrawer({
  app,
  canManage,
  canPublish,
  onClose,
  builds = [],
  publishes = [],
  loading = false,
  onFetch,
  fetching = false,
  onTestJenkins,
  testingJenkins = false,
  onEdit,
  onDelete,
  onDownload,
  downloadingBuildId,
  onDeleteBuild,
  deletingBuildId,
  onPublishBuild,
}) {
  if (!app) return null;

  const activeBuilds = builds.filter((b) => ACTIVE_BUILD_STATUSES.has(b.status)).length;

  return (
    <>
      <div className="sg-ma-scrim" role="presentation" onClick={onClose} />
      <aside className="sg-ma-drawer" role="dialog" aria-modal="true" aria-label={`${app.name} details`}>
        <header className="sg-ma-dr-head">
          <div className="sg-ma-dr-top">
            <h3 className="sg-ma-dr-title">{app.name}</h3>
            <span className={`status-pill ${app.enabled ? "ok" : "muted"}`}>
              {app.enabled ? "Enabled" : "Disabled"}
            </span>
            <button
              type="button"
              className="icon-button sg-ma-dr-close"
              onClick={onClose}
              aria-label="Close"
            >
              <IconClose />
            </button>
          </div>
          {app.description ? <p className="sg-ma-dr-desc">{app.description}</p> : null}

          <div className="sg-ma-dr-facts">
            <div className="sg-ma-fact">
              <span className="sg-ma-fact-k">Platforms</span>
              <span className="sg-ma-fact-v sg-ma-plats">
                {(app.platforms || []).length ? (
                  app.platforms.map((p) => <PlatformBadge key={p} platform={p} />)
                ) : (
                  <span className="muted">none</span>
                )}
              </span>
            </div>
            <div className="sg-ma-fact">
              <span className="sg-ma-fact-k">Zoho environment</span>
              <span className="sg-ma-fact-v">
                {app.zohoEnvironment ? <span className="sg-tag">{app.zohoEnvironment}</span> : <span className="muted">—</span>}
              </span>
            </div>
            <div className="sg-ma-fact">
              <span className="sg-ma-fact-k">Jenkins job</span>
              <span className="sg-ma-fact-v mono sg-ma-wrap">{app.jenkinsJobPath || "—"}</span>
            </div>
            <div className="sg-ma-fact">
              <span className="sg-ma-fact-k">Store credentials</span>
              <span className="sg-ma-fact-v sg-ma-creds">
                <CredPill label="Play" configured={app.playServiceAccountConfigured} />
                <CredPill label="ASC" configured={app.ascPrivateKeyConfigured} />
              </span>
            </div>
          </div>

          {app.lastTestStatus ? (
            <p className={`sg-ma-testline ${app.lastTestStatus === "ok" ? "" : "sg-ma-testline--err"}`}>
              <TestStatusPill status={app.lastTestStatus} />
              <span>{app.lastTestMessage || (app.lastTestStatus === "ok" ? "Last test passed." : "Last test failed.")}</span>
            </p>
          ) : null}

          {canManage ? (
            <div className="sg-ma-dr-actions">
              <button type="button" className="secondary" onClick={onFetch} disabled={fetching}>
                {fetching ? "Fetching…" : "Fetch latest build"}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={onTestJenkins}
                disabled={testingJenkins}
              >
                {testingJenkins ? "Testing…" : "Test Jenkins"}
              </button>
              <button type="button" className="btn-outline" onClick={onEdit}>
                Edit
              </button>
              <button type="button" className="btn-ghost sg-ma-del-app" onClick={onDelete}>
                <IconTrash width={14} height={14} />
                Delete
              </button>
            </div>
          ) : null}
        </header>

        <div className="sg-ma-dr-body">
          {/* ── Builds ─────────────────────────────────────────────── */}
          <section className="sg-ma-dr-sect">
            <div className="sg-ma-dr-secthead">
              <h4>Builds</h4>
              <span className="sg-ma-count">
                {builds.length}
                {activeBuilds ? ` · ${activeBuilds} active` : ""}
              </span>
            </div>
            {loading && !builds.length ? (
              <p className="muted">Loading builds…</p>
            ) : builds.length ? (
              <ul className="sg-ma-builds">
                {builds.map((build) => (
                  <BuildRow
                    key={build.id}
                    build={build}
                    canManage={canManage}
                    canPublish={canPublish}
                    onDownload={onDownload}
                    downloading={downloadingBuildId === build.id}
                    onDeleteBuild={onDeleteBuild}
                    deleting={deletingBuildId === build.id}
                    onPublishBuild={onPublishBuild}
                  />
                ))}
              </ul>
            ) : (
              <p className="muted sg-ma-empty-hint">
                No builds yet.{" "}
                {canManage ? "Use “Fetch latest build” to pull the newest Jenkins artifact." : null}
              </p>
            )}
          </section>

          {/* ── Publish history ────────────────────────────────────── */}
          <section className="sg-ma-dr-sect">
            <div className="sg-ma-dr-secthead">
              <h4>Publish history</h4>
              <span className="sg-ma-count">{publishes.length}</span>
            </div>
            {publishes.length ? (
              <ul className="sg-ma-pubs">
                {publishes.map((publish) => (
                  <PublishCard key={publish.id} publish={publish} />
                ))}
              </ul>
            ) : (
              <p className="muted sg-ma-empty-hint">Nothing published yet.</p>
            )}
          </section>
        </div>
      </aside>
    </>
  );
}
