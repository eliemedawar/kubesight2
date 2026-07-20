import { useMemo, useState } from "react";
import {
  ACTIVE_PUBLISH_STATUSES,
  ACTIVE_RESIGN_STATUSES,
  AppAvatar,
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
  ResignStatusPill,
  ResignSteps,
  storeReadiness,
  storeTargetLabel,
  TestStatusPill,
  timeAgo,
} from "./common.jsx";

// "editId 12345 · versionCode 42" — storeRef is a small dict of store handles.
function storeRefText(storeRef) {
  return Object.entries(storeRef || {})
    .filter(([, v]) => v !== "" && v !== null && v !== undefined)
    .map(([k, v]) => `${k} ${v}`)
    .join(" · ");
}

// One publish attempt, nested under the build it shipped. Step chips only
// while something is still moving (or went wrong) — a clean "Published" row
// doesn't need its four green chips repeated.
function PublishEntry({ publish }) {
  const active = ACTIVE_PUBLISH_STATUSES.has(publish.status);
  const failed = publish.status === "failed";
  const tone = publish.status === "published" ? "ok" : failed ? "fail" : active ? "run" : "wait";
  const refText = publish.status === "published" ? storeRefText(publish.storeRef) : "";
  return (
    <li className={`sg-ma-rpub sg-ma-rpub--${tone}`}>
      <div className="sg-ma-rpub-line">
        <b>{storeTargetLabel(publish.store, publish.target)}</b>
        <PublishStatusPill status={publish.status} />
        {publish.triggeredBy ? <span className="sg-ma-rpub-who">by {publish.triggeredBy}</span> : null}
        <span className="sg-ma-rpub-when">{timeAgo(publish.finishedAt || publish.createdAt)}</span>
      </div>
      {active || failed ? <PublishSteps steps={publish.steps} /> : null}
      {failed && publish.error ? <p className="sg-ma-inline-error">{publish.error}</p> : null}
      {refText ? <p className="sg-ma-rpub-ref mono">{refText}</p> : null}
    </li>
  );
}

// One signing attempt, nested under the build it was signing. Same treatment
// as a publish: chips only while it matters.
function ResignEntry({ resign }) {
  const active = ACTIVE_RESIGN_STATUSES.has(resign.status);
  const failed = resign.status === "failed";
  const tone = resign.status === "completed" ? "ok" : failed ? "fail" : active ? "run" : "wait";
  return (
    <li className={`sg-ma-rpub sg-ma-rpub--${tone}`}>
      <div className="sg-ma-rpub-line">
        <b>Re-sign</b>
        <ResignStatusPill status={resign.status} />
        {resign.triggeredBy ? <span className="sg-ma-rpub-who">by {resign.triggeredBy}</span> : null}
        <span className="sg-ma-rpub-when">{timeAgo(resign.finishedAt || resign.createdAt)}</span>
      </div>
      {active || failed ? <ResignSteps steps={resign.steps} /> : null}
      {failed && resign.error ? <p className="sg-ma-inline-error">{resign.error}</p> : null}
    </li>
  );
}

// A build and everything that happened to it: metadata, artifact actions,
// and its publish attempts nested underneath.
function ReleaseCard({
  build,
  publishes = [],
  resigns = [],
  canManage,
  canPublish,
  canResign,
  onDownload,
  downloading,
  onDeleteBuild,
  deleting,
  onPublishBuild,
  onResignBuild,
  resigning,
}) {
  const available = build.status === "available";
  // Shielding strips the code signature, so a shielded binary can never be
  // published as-is — the backend refuses it too. Surface that on the card
  // rather than letting the operator find out from a failed publish.
  const unsigned = build.signatureState === "unsigned";
  // Only offer signing where KubeSight can actually do it, and not while a job
  // for this build is already in flight.
  const signingInFlight = resigns.some((r) => ACTIVE_RESIGN_STATUSES.has(r.status));
  const showResign =
    canResign && available && unsigned && build.platform === "android" && !signingInFlight;
  return (
    <li className="sg-ma-rel">
      <div className="sg-ma-rel-build">
        <div className="sg-ma-rel-main">
          <div className="sg-ma-rel-head">
            <PlatformBadge platform={build.platform} />
            <b className="sg-ma-rel-ver">{build.version || "—"}</b>
            <span className="sg-tag">{(build.artifactType || "").toUpperCase()}</span>
            <BuildStatusPill status={build.status} />
            {build.source === "manual" ? <span className="sg-ma-count">manual</span> : null}
            {build.source === "upload" ? <span className="sg-ma-count">uploaded</span> : null}
            {unsigned ? (
              <span
                className="sg-ma-unsigned"
                title="This binary has no code signature — shielding strips it. Re-sign before publishing."
              >
                Unsigned
              </span>
            ) : null}
          </div>
          <div className="sg-ma-rel-meta">
            {build.fileName ? (
              <span className="sg-ma-rel-file" title={build.fileName}>
                {build.fileName}
              </span>
            ) : null}
            {build.fileSize ? (
              <>
                <span className="sg-ma-dot" aria-hidden="true">
                  ·
                </span>
                <span>{formatBytes(build.fileSize)}</span>
              </>
            ) : null}
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
        <div className="sg-ma-rel-acts">
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
          {showResign ? (
            <button
              type="button"
              className="secondary sg-ma-resignbtn"
              onClick={() => onResignBuild(build)}
              disabled={resigning}
              title="Sign this binary with the upload key"
            >
              {resigning ? "Starting…" : "Re-sign"}
            </button>
          ) : null}
          {canPublish && available ? (
            <button
              type="button"
              className="btn-outline sg-ma-pubbtn"
              onClick={() => onPublishBuild(build)}
              disabled={unsigned}
              title={
                unsigned
                  ? "Unsigned binary — re-sign it before publishing"
                  : undefined
              }
            >
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
      </div>
      {resigns.length || publishes.length ? (
        <ul className="sg-ma-rel-pubs">
          {resigns.map((resign) => (
            <ResignEntry key={`r${resign.id}`} resign={resign} />
          ))}
          {publishes.map((publish) => (
            <PublishEntry key={`p${publish.id}`} publish={publish} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

// Artifact resolution summary line for one platform, from artifactConfig.
// Workspace files vanish when Jenkins cleans the workspace — flag them.
function ArtifactRow({ platform, config }) {
  const label = platform === "android" ? "Android artifact" : "iOS artifact";
  if (!config) {
    return (
      <div className="sg-ma-cfg-row">
        <span className="sg-ma-cfg-k">{label}</span>
        <span className="sg-ma-cfg-v muted">not configured</span>
      </div>
    );
  }
  const workspace = config.source === "workspace";
  return (
    <div className="sg-ma-cfg-row">
      <span className="sg-ma-cfg-k">{label}</span>
      <span className="sg-ma-cfg-v mono">
        {workspace ? `workspace · ${config.path || "—"}` : `archived · ${config.pattern || "*"}`}
      </span>
      {workspace ? (
        <span
          className="sg-ma-cfg-flag"
          title="Workspace files are deleted when the Jenkins workspace is cleaned — archived artifacts are more reliable."
        >
          fragile
        </span>
      ) : null}
    </div>
  );
}

const READINESS_STATE = {
  ready: ["sg-ma-cfg-ok", "✓ Ready"],
  missing: ["sg-ma-cfg-warn", "Key missing"],
  off: ["muted", "Not shipped"],
};

export default function AppDrawer({
  app,
  canManage,
  canPublish,
  onClose,
  builds = [],
  publishes = [],
  resigns = [],
  loading = false,
  onFetch,
  fetching = false,
  onUpload,
  onTestJenkins,
  testingJenkins = false,
  onEdit,
  onDelete,
  onDownload,
  downloadingBuildId,
  onDeleteBuild,
  deletingBuildId,
  onPublishBuild,
  onResignBuild,
  resigningBuildId,
}) {
  const [tab, setTab] = useState("releases");

  // Publishes grouped under the build that shipped them; a publish whose build
  // has since been deleted still shows up in a trailing "earlier" section.
  const pubsByBuild = useMemo(() => {
    const map = new Map();
    publishes.forEach((p) => {
      const list = map.get(p.buildId) || [];
      list.push(p);
      map.set(p.buildId, list);
    });
    return map;
  }, [publishes]);
  const orphanPublishes = useMemo(() => {
    const buildIds = new Set(builds.map((b) => b.id));
    return publishes.filter((p) => !buildIds.has(p.buildId));
  }, [builds, publishes]);

  // Signing attempts hang off the build they were signing, same as publishes.
  const resignsByBuild = useMemo(() => {
    const map = new Map();
    resigns.forEach((r) => {
      const list = map.get(r.buildId) || [];
      list.push(r);
      map.set(r.buildId, list);
    });
    return map;
  }, [resigns]);

  // Whether re-signing is set up at all for this app — no point offering a
  // button that can only return "no re-signing setup".
  const canResign = canManage && Boolean((app?.resignConfig || {}).android);

  if (!app) return null;

  const artifactConfig = app.artifactConfig || {};
  const readiness = storeReadiness(app);
  const playState = readiness.find((r) => r.key === "google_play")?.state || "off";
  const ascState = readiness.find((r) => r.key === "app_store")?.state || "off";

  return (
    <>
      <div className="sg-ma-scrim" role="presentation" onClick={onClose} />
      <aside className="sg-ma-drawer" role="dialog" aria-modal="true" aria-label={`${app.name} details`}>
        <header className="sg-ma-dr-head">
          <div className="sg-ma-dr-top">
            <AppAvatar name={app.name} enabled={app.enabled} />
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

          <div className="sg-ma-dr-chips">
            {(app.platforms || []).map((p) => (
              <PlatformBadge key={p} platform={p} />
            ))}
            {app.zohoEnvironment ? <span className="sg-tag">{app.zohoEnvironment}</span> : null}
            {app.jenkinsJobPath ? (
              <span className="sg-ma-count sg-ma-jobchip" title="Jenkins job">
                {app.jenkinsJobPath}
              </span>
            ) : null}
          </div>

          {canManage ? (
            <div className="sg-ma-dr-actions">
              <button type="button" className="secondary" onClick={onFetch} disabled={fetching}>
                {fetching ? "Fetching…" : "Fetch latest build"}
              </button>
              {onUpload ? (
                <button type="button" className="secondary" onClick={onUpload}>
                  Upload build
                </button>
              ) : null}
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
              <button
                type="button"
                className="icon-button sg-ma-danger-icon sg-ma-del-app"
                onClick={onDelete}
                title="Delete application"
                aria-label="Delete application"
              >
                <IconTrash />
              </button>
            </div>
          ) : null}
        </header>

        <nav className="sg-ma-dr-tabs" aria-label="Application details">
          <button
            type="button"
            className={`sg-ma-dr-tab${tab === "releases" ? " sg-ma-dr-tab--on" : ""}`}
            onClick={() => setTab("releases")}
            aria-pressed={tab === "releases"}
          >
            Releases
            <span className="sg-ma-count">{builds.length}</span>
          </button>
          <button
            type="button"
            className={`sg-ma-dr-tab${tab === "setup" ? " sg-ma-dr-tab--on" : ""}`}
            onClick={() => setTab("setup")}
            aria-pressed={tab === "setup"}
          >
            Setup
          </button>
        </nav>

        <div className="sg-ma-dr-body">
          {tab === "releases" ? (
            loading && !builds.length ? (
              <p className="muted">Loading builds…</p>
            ) : builds.length ? (
              <>
                <ul className="sg-ma-rels">
                  {builds.map((build) => (
                    <ReleaseCard
                      key={build.id}
                      build={build}
                      publishes={pubsByBuild.get(build.id) || []}
                      resigns={resignsByBuild.get(build.id) || []}
                      canManage={canManage}
                      canPublish={canPublish}
                      canResign={canResign}
                      onDownload={onDownload}
                      downloading={downloadingBuildId === build.id}
                      onDeleteBuild={onDeleteBuild}
                      deleting={deletingBuildId === build.id}
                      onPublishBuild={onPublishBuild}
                      onResignBuild={onResignBuild}
                      resigning={resigningBuildId === build.id}
                    />
                  ))}
                </ul>
                {orphanPublishes.length ? (
                  <section className="sg-ma-dr-sect">
                    <div className="sg-ma-dr-secthead">
                      <h4>Earlier publishes</h4>
                      <span className="sg-ma-count">{orphanPublishes.length}</span>
                    </div>
                    <ul className="sg-ma-rel-pubs sg-ma-rel-pubs--orphan">
                      {orphanPublishes.map((publish) => (
                        <PublishEntry key={publish.id} publish={publish} />
                      ))}
                    </ul>
                  </section>
                ) : null}
              </>
            ) : (
              <p className="muted sg-ma-empty-hint">
                No builds yet.{" "}
                {canManage
                  ? "Use “Fetch latest build” to pull the newest Jenkins artifact, or “Upload build” to add an APK/AAB/IPA directly."
                  : null}
              </p>
            )
          ) : (
            <>
              <section className="sg-ma-cfg">
                <h5>Build source</h5>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">Zoho environment</span>
                  <span className="sg-ma-cfg-v">
                    {app.zohoEnvironment ? (
                      <span className="sg-tag">{app.zohoEnvironment}</span>
                    ) : (
                      <span className="muted">not linked</span>
                    )}
                  </span>
                </div>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">Jenkins job</span>
                  <span className="sg-ma-cfg-v mono">{app.jenkinsJobPath || "—"}</span>
                </div>
                <ArtifactRow platform="android" config={artifactConfig.android} />
                <ArtifactRow platform="ios" config={artifactConfig.ios} />
                {app.lastTestStatus ? (
                  <p className={`sg-ma-testline ${app.lastTestStatus === "ok" ? "" : "sg-ma-testline--err"}`}>
                    <TestStatusPill status={app.lastTestStatus} />
                    <span>
                      {app.lastTestMessage ||
                        (app.lastTestStatus === "ok" ? "Last test passed." : "Last test failed.")}
                    </span>
                  </p>
                ) : null}
                {canManage ? (
                  <div className="sg-ma-cfg-foot">
                    <button
                      type="button"
                      className="secondary"
                      onClick={onTestJenkins}
                      disabled={testingJenkins}
                    >
                      {testingJenkins ? "Testing…" : "Test Jenkins"}
                    </button>
                  </div>
                ) : null}
              </section>

              <section className="sg-ma-cfg">
                <h5>Google Play</h5>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">Package</span>
                  <span className="sg-ma-cfg-v mono">{app.androidPackageName || "—"}</span>
                </div>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">Service account</span>
                  <span className="sg-ma-cfg-v">
                    {app.playServiceAccountConfigured ? "Stored key · write-only" : "No key stored"}
                  </span>
                  <span className={`sg-ma-cfg-st ${READINESS_STATE[playState][0]}`}>
                    {READINESS_STATE[playState][1]}
                  </span>
                </div>
                {canManage ? (
                  <div className="sg-ma-cfg-foot">
                    <button type="button" className="secondary" onClick={onEdit}>
                      {app.playServiceAccountConfigured ? "Edit credentials" : "Add credentials"}
                    </button>
                  </div>
                ) : null}
              </section>

              <section className="sg-ma-cfg">
                <h5>App Store Connect</h5>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">Bundle ID</span>
                  <span className="sg-ma-cfg-v mono">{app.iosBundleId || "—"}</span>
                </div>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">ASC app ID</span>
                  <span className="sg-ma-cfg-v mono">{app.ascAppId || "—"}</span>
                </div>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">Issuer / key</span>
                  <span className="sg-ma-cfg-v mono">
                    {app.ascIssuerId || "—"} / {app.ascKeyId || "—"}
                  </span>
                </div>
                <div className="sg-ma-cfg-row">
                  <span className="sg-ma-cfg-k">API key</span>
                  <span className="sg-ma-cfg-v">
                    {app.ascPrivateKeyConfigured ? "Stored key · write-only" : "No private key stored"}
                  </span>
                  <span className={`sg-ma-cfg-st ${READINESS_STATE[ascState][0]}`}>
                    {READINESS_STATE[ascState][1]}
                  </span>
                </div>
                {canManage ? (
                  <div className="sg-ma-cfg-foot">
                    <button type="button" className="secondary" onClick={onEdit}>
                      {app.ascPrivateKeyConfigured ? "Edit credentials" : "Add credentials"}
                    </button>
                  </div>
                ) : null}
              </section>
            </>
          )}
        </div>
      </aside>
    </>
  );
}
