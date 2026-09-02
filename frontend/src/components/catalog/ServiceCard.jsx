import {
  BranchIcon,
  PackageIcon,
  PlayIcon,
  RepoIcon,
  Sparkline,
  StatusPill,
  applicationTypeLabel,
  formatDuration,
  formatRelative,
  isBuildActive,
} from "./ciShared.jsx";

/**
 * One service in the catalog grid — a verdict, not a record.
 *
 * One dominant signal (the latest build's outcome), one verdict line saying
 * where and when, a sparkline for trend, and the two actions that matter:
 * open, and Run (hover/focus-revealed). A service that cannot build yet is
 * drawn dashed and its warning chip IS the fix — it deep-links to the tab
 * where the gap is closed.
 */
export default function ServiceCard({ service, onOpen, onOpenBuild, onRun, canRun }) {
  const build = service.latestBuild;
  const needsSource = !service.sourceConfigured;
  const needsPipeline = !service.pipelineConfigured;
  const needsSetup = needsSource || needsPipeline;
  const buildActive = Boolean(build && isBuildActive(build.status));

  const iconTone =
    service.criticality === "critical" || service.criticality === "high"
      ? "sg-ico--accent"
      : "sg-ico--muted";

  const handleKeyDown = (event) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen();
    }
    if ((event.key === "r" || event.key === "R") && canRun && !needsSetup && !buildActive) {
      event.preventDefault();
      onRun();
    }
  };

  const verdict = () => {
    if (!build) return `Registered ${formatRelative(service.createdAt)}`;
    if (build.status === "running") {
      return build.currentStage
        ? `stage ${build.stageProgress} · ${build.currentStage}`
        : "starting…";
    }
    if (build.status === "queued") return build.queueReason || "waiting for a runner";
    if ((build.status === "failed" || build.status === "timeout") && build.failedStage) {
      return `failed in ${build.failedStage} · ${formatRelative(build.finishedAt)}`;
    }
    return `${build.status} ${formatRelative(build.finishedAt || build.queuedAt)}${
      build.branch ? ` on ${build.branch}` : ""
    }`;
  };

  return (
    <article
      className={`sg-ccard sg-ccard--clickable sg-ci-card${
        needsSetup ? " sg-ci-card--setup" : ""
      }`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={handleKeyDown}
      aria-label={`Open service ${service.name}`}
    >
      <header>
        <span className={`sg-ico ${iconTone}`}>
          <PackageIcon />
        </span>
        <div className="sg-ci-card-id">
          <b>{service.name}</b>
          <span className="sg-ccard-sub">
            {applicationTypeLabel(service.applicationType)}
            {service.ownerTeam ? ` · ${service.ownerTeam}` : ""}
          </span>
        </div>
        {build ? (
          <StatusPill status={build.status}>{`#${build.number} ${build.status}`}</StatusPill>
        ) : (
          <span className="status-pill unknown">no builds</span>
        )}
      </header>

      <div className="sg-ci-card-source">
        {service.sourceConfigured ? (
          <>
            <span className="sg-ci-meta" title={service.repositoryUrl}>
              <RepoIcon />
              {service.repositoryWorkspace}/{service.repositoryName}
            </span>
            <span className="sg-ci-meta">
              <BranchIcon />
              {service.defaultBranch}
            </span>
          </>
        ) : (
          <span className="sg-ci-meta sg-ci-meta--todo">
            <RepoIcon />
            No repository connected
          </span>
        )}
      </div>

      {/* Verdict line: outcome + where + when. Clicking it opens the build's
          drawer directly — a red card is a question, this is the answer. */}
      <div className="sg-ci-card-build">
        <Sparkline statuses={service.recentBuildStatuses} />
        {build ? (
          <button
            type="button"
            className={`sg-ci-verdict sg-ci-verdict--${build.status}`}
            onClick={(event) => {
              event.stopPropagation();
              onOpenBuild(build.id);
            }}
            title={`Open build #${build.number}`}
          >
            {verdict()}
          </button>
        ) : (
          <span className="muted sg-ci-verdict-text">{verdict()}</span>
        )}
        {build?.durationSeconds != null && (
          <span className="sg-ci-duration">{formatDuration(build.durationSeconds)}</span>
        )}
      </div>

      <footer>
        {needsSource && (
          <button
            type="button"
            className="sg-tag sg-ci-tag--todo sg-ci-tag--action"
            onClick={(event) => {
              event.stopPropagation();
              onOpen("source");
            }}
          >
            Source needed → Connect
          </button>
        )}
        {!needsSource && needsPipeline && (
          <button
            type="button"
            className="sg-tag sg-ci-tag--todo sg-ci-tag--action"
            onClick={(event) => {
              event.stopPropagation();
              onOpen("pipeline");
            }}
          >
            Pipeline needed → Configure
          </button>
        )}
        {!needsSetup && service.latestArtifact && (
          <span
            className="sg-tag sg-ci-tag--artifact"
            title={service.latestArtifact.uri || service.latestArtifact.name}
          >
            {service.latestArtifact.artifactType}
            {service.latestArtifact.version ? ` · v${service.latestArtifact.version}` : ""}
          </span>
        )}
        {build?.triggerType === "automation" && (
          <span className="sg-tag sg-ci-tag--auto">automation</span>
        )}
        {service.status !== "active" && (
          <span className="sg-tag sg-ci-tag--todo">{service.status}</span>
        )}
        {canRun && !needsSetup && service.status === "active" && (
          <button
            type="button"
            className="sg-ci-card-run"
            disabled={buildActive}
            title={
              buildActive
                ? `Build #${build.number} is ${build.status}`
                : "Run a build — pick a branch or tag (R)"
            }
            onClick={(event) => {
              event.stopPropagation();
              if (!buildActive) onRun();
            }}
          >
            <PlayIcon />
            Run
          </button>
        )}
      </footer>
    </article>
  );
}
