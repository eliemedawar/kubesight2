import PipelineStrip from "./PipelineStrip.jsx";
import {
  BranchIcon,
  CheckIcon,
  RepoIcon,
  StatusPill,
  XIcon,
  applicationTypeLabel,
  formatDuration,
  formatRelative,
  shortSha,
} from "./ciShared.jsx";

/**
 * Overview tab: is this service wired up, and what happened recently.
 *
 * The readiness list is the first thing shown because an unconfigured service
 * should say what is missing rather than presenting an inert Run Build button.
 */
export default function ServiceOverview({ summary, stages, onOpenBuild, onGoToTab }) {
  const { service, readiness, recentBuilds = [], recentArtifacts = [], stats } = summary;

  return (
    <div className="sg-ci-panel">
      {!readiness.ready && (
        <section className="sg-ci-readiness">
          <p className="form-label">Before this service can build</p>
          <ul>
            {readiness.checks.map((check) => (
              <li key={check.key} className={check.ok ? "is-ok" : "is-todo"}>
                <span className="sg-ci-readiness-icon">
                  {check.ok ? <CheckIcon /> : <XIcon />}
                </span>
                <span>{check.label}</span>
                {!check.ok && <span className="muted">{check.hint}</span>}
                {!check.ok && check.key === "source" && (
                  <button
                    type="button"
                    className="btn-outline btn-compact"
                    onClick={() => onGoToTab("source")}
                  >
                    Connect
                  </button>
                )}
                {!check.ok && check.key === "pipeline" && (
                  <button
                    type="button"
                    className="btn-outline btn-compact"
                    onClick={() => onGoToTab("pipeline")}
                  >
                    Configure
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="sg-ci-stat-row">
        <div className="sg-ci-stat">
          <span className="sg-ci-stat-value">{stats.totalBuilds}</span>
          <span className="sg-ci-stat-label">builds</span>
        </div>
        <div className="sg-ci-stat">
          <span className="sg-ci-stat-value">
            {stats.successRate === null ? "—" : `${stats.successRate}%`}
          </span>
          <span className="sg-ci-stat-label">success rate</span>
        </div>
        <div className="sg-ci-stat">
          <span className="sg-ci-stat-value">{stats.failed}</span>
          <span className="sg-ci-stat-label">failed</span>
        </div>
        <div className="sg-ci-stat">
          <span className="sg-ci-stat-value">{recentArtifacts.length ? recentArtifacts[0].artifactType : "—"}</span>
          <span className="sg-ci-stat-label">latest artifact</span>
        </div>
      </section>

      <section className="form-section">
        <h4>Identity</h4>
        <dl className="sg-ci-dl">
          <div>
            <dt>Type</dt>
            <dd>{applicationTypeLabel(service.applicationType)}</dd>
          </div>
          <div>
            <dt>Owner</dt>
            <dd>{service.ownerTeam || "—"}</dd>
          </div>
          <div>
            <dt>Criticality</dt>
            <dd>{service.criticality || "—"}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>
              <StatusPill status={service.status} />
            </dd>
          </div>
          <div>
            <dt>Repository</dt>
            <dd>
              {service.sourceConfigured ? (
                <span className="sg-ci-meta">
                  <RepoIcon />
                  {service.repositoryWorkspace}/{service.repositoryName}
                </span>
              ) : (
                <span className="muted">not connected</span>
              )}
            </dd>
          </div>
          <div>
            <dt>Default branch</dt>
            <dd>
              <span className="sg-ci-meta">
                <BranchIcon />
                {service.defaultBranch}
              </span>
            </dd>
          </div>
        </dl>
      </section>

      {(summary.latestBuildStages?.length > 0 || stages?.length > 0) && (
        <section className="form-section">
          <h4>{summary.latestBuildStages?.length ? "Last build" : "Pipeline"}</h4>
          {/* The strip is the LAST BUILD's truth when one exists — which stages
              passed, which failed, which were skipped. Clicking a node opens
              that build's drawer. The definition lives on the Pipeline tab. */}
          {summary.latestBuildStages?.length ? (
            <PipelineStrip
              stages={summary.latestBuildStages}
              onSelectStage={() => onOpenBuild(summary.latestBuildId)}
            />
          ) : (
            <PipelineStrip stages={stages} />
          )}
        </section>
      )}

      {recentBuilds.length > 0 && (
        <section className="form-section">
          <h4>Recent builds</h4>
          <ul className="sg-ci-recent">
            {recentBuilds.map((build) => (
              <li key={build.id}>
                <button type="button" onClick={() => onOpenBuild(build.id)}>
                  <strong>#{build.number}</strong>
                  <span className="muted">{build.branch || "—"}</span>
                  <code>{shortSha(build.commitSha)}</code>
                  <StatusPill status={build.status} />
                  <span className="sg-ci-duration">
                    {formatDuration(build.durationSeconds)}
                  </span>
                  <span className="muted">
                    {formatRelative(build.finishedAt || build.queuedAt)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
