import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import InfoCard from "../components/common/InfoCard.jsx";
import { EMPTY_MESSAGES } from "../utils/authz.js";
import { mapPrecheckClass, mapPrecheckState } from "../utils/formatters.js";

function DetailRow({ label, value, children, stacked = false }) {
  return (
    <div className={`upgrade-detail-row${stacked ? " upgrade-detail-row--control" : ""}`}>
      <span className="upgrade-detail-label">{label}</span>
      <span className="upgrade-detail-value">{children ?? value ?? "—"}</span>
    </div>
  );
}

function StatusBadge({ status }) {
  const normalized = mapPrecheckState(status);
  const className = mapPrecheckClass(status);
  return <span className={`status-badge status-badge--${className}`}>{normalized}</span>;
}

function SupportBadge({ supported }) {
  return (
    <span className={`status-badge status-badge--${supported ? "pass" : "warning"}`}>
      {supported ? "Yes" : "No"}
    </span>
  );
}

export default function UpgradeSafeModePage({
  upgradeData,
  targetVersion,
  onTargetVersionChange,
  onRunPrecheck,
  onStartUpgrade,
  onViewInstructions,
  loading,
  coreLoading = false,
  hasClusters = true,
  accessError = "",
  canPrecheck,
  canStart,
}) {
  const clusterInfo = upgradeData?.clusterInfo;
  const provider = upgradeData?.provider;
  const versionInfo = upgradeData?.versionInfo;
  const versionSkew = upgradeData?.versionSkew;
  const upgradePlan = upgradeData?.upgradePlan;
  const checks = upgradeData?.upgradeChecks || [];
  const instructions = upgradeData?.instructions || provider?.instructions;
  const showsInstructionsOnly =
    provider?.executionMode === "instructions" ||
    (!provider?.upgradeSupported &&
      provider?.executionMode !== "plan-only" &&
      provider?.executionMode !== "execute-with-cli");
  const manualRequired = upgradePlan?.manualUpgradeRequired ?? showsInstructionsOnly;
  const executionSupported =
    upgradeData?.executionSupported === true || provider?.executionMode === "execute-with-cli";
  const upgradeRunning = upgradeData?.status === "running";
  const upgradeFailed = upgradeData?.status === "failed";
  const upgradeCompleted = upgradeData?.status === "completed";
  const stepsToRender =
    (upgradeData?.upgradeSteps?.length ? upgradeData.upgradeSteps : upgradePlan?.steps) || [];

  const latestDisplay =
    versionInfo?.latestAvailable && versionInfo.latestAvailable !== "unknown"
      ? versionInfo.latestAvailable
      : "Unknown";

  const planGenerated =
    upgradeData?.status === "manual_required" && Boolean(upgradeData?.upgradeId);
  const actionLabel = showsInstructionsOnly
    ? "View Upgrade Instructions"
    : executionSupported && !manualRequired
      ? "Start Automatic Upgrade"
      : manualRequired || !executionSupported
        ? "Generate Upgrade Plan"
        : "Start Upgrade Plan";

  const handlePrimaryAction = () => {
    if (showsInstructionsOnly) {
      onViewInstructions?.();
      return;
    }
    onStartUpgrade?.();
  };

  const header = (
    <PageTitle
      title="Kubernetes Upgrade Management"
      subtitle={
        upgradeData?.message ||
        "Cluster intelligence, readiness prechecks, and provider-aware upgrade workflows."
      }
    />
  );

  return (
    <AccessScopeView
      coreLoading={coreLoading}
      pageLoading={loading && !upgradeData}
      accessError={accessError}
      empty={!hasClusters}
      emptyMessage={EMPTY_MESSAGES.noClusters}
      loadingLabel={coreLoading ? "Loading clusters..." : "Loading upgrade data..."}
      header={header}
    >
      {upgradeData?.canUpgrade === false ? (
        <p className="banner-message error">
          Precheck failed — resolve failed checks before proceeding.
        </p>
      ) : null}
      {upgradeData?.canUpgrade === true && !upgradeData?.status ? (
        <p className="banner-message">Precheck passed — cluster is ready for upgrade planning.</p>
      ) : null}
      {upgradeData?.status === "confirmation_required" ? (
        <div className="banner-message warning-banner upgrade-result-banner" role="status">
          <strong>Confirmation required.</strong>{" "}
          {upgradeData.message || "Type the confirmation phrase below to generate the upgrade plan."}
        </div>
      ) : null}
      {planGenerated ? (
        <div className="banner-message upgrade-result-banner" role="status">
          <strong>Upgrade plan generated.</strong> Reference ID:{" "}
          <code>{upgradeData.upgradeId}</code>. KubeSight does not run cluster upgrades
          automatically — complete the manual steps in the upgrade plan and instructions below.
        </div>
      ) : upgradeRunning ? (
        <div className="banner-message warning-banner upgrade-result-banner" role="status">
          <strong>Automatic upgrade in progress…</strong>{" "}
          {upgradeData.message || "KubeSight is running kubeadm upgrade steps on the cluster."}
        </div>
      ) : upgradeCompleted ? (
        <div className="banner-message upgrade-result-banner" role="status">
          <strong>Upgrade completed.</strong> {upgradeData.message}
        </div>
      ) : upgradeFailed ? (
        <p className="banner-message error upgrade-result-banner" role="status">
          <strong>Upgrade failed.</strong> {upgradeData.error || upgradeData.message}
        </p>
      ) : upgradeData?.status === "manual_required" ? (
        <p className="banner-message warning-banner upgrade-result-banner" role="status">
          Manual upgrade required — KubeSight does not execute provider upgrades automatically.
        </p>
      ) : null}

      <section className="upgrade-top-grid content-grid">
        <InfoCard title="Cluster Information">
          {clusterInfo ? (
            <div className="upgrade-details">
              <DetailRow label="Cluster" value={clusterInfo.contextName || clusterInfo.clusterId} />
              <DetailRow label="Provider" value={clusterInfo.providerDisplay} />
              <DetailRow label="Control Plane" value={clusterInfo.controlPlaneVersion} />
              <DetailRow label="kubectl Client" value={clusterInfo.kubectlClientVersion} />
              <div className="upgrade-detail-row upgrade-detail-row--stack">
                <span className="upgrade-detail-label">Nodes</span>
                <ul className="upgrade-node-list">
                  {(clusterInfo.nodes || []).map((node) => (
                    <li key={node.name}>
                      <span>{node.name}</span>
                      <span className="muted">{node.version}</span>
                      {!node.ready ? <span className="status-badge status-badge--fail">Not Ready</span> : null}
                    </li>
                  ))}
                </ul>
              </div>
              <DetailRow label="Health">
                <span
                  className={`status-badge status-badge--${
                    clusterInfo.health === "healthy" ? "pass" : clusterInfo.health === "unhealthy" ? "fail" : "warning"
                  }`}
                >
                  {clusterInfo.health === "healthy"
                    ? "Healthy"
                    : clusterInfo.health === "unhealthy"
                      ? "Unhealthy"
                      : "Unknown"}
                </span>
              </DetailRow>
            </div>
          ) : (
            <p className="muted">Select a cluster to load cluster information.</p>
          )}
        </InfoCard>

        <InfoCard title="Version Information">
          {versionInfo ? (
            <div className="upgrade-details">
              <DetailRow label="Current Version" value={versionInfo.currentVersion} />
              <DetailRow label="Latest Available" value={latestDisplay} />
              <DetailRow label="Target Version" stacked>
                <input
                  list="upgrade-version-options"
                  className="upgrade-version-select"
                  value={targetVersion}
                  onChange={(event) => onTargetVersionChange?.(event.target.value)}
                  disabled={loading}
                  placeholder="e.g. v1.33.0"
                />
                <datalist id="upgrade-version-options">
                  {(versionInfo.targetOptions || []).length > 0
                    ? versionInfo.targetOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))
                    : [
                        latestDisplay !== "Unknown" ? latestDisplay : null,
                        versionInfo.currentVersion || null,
                      ]
                        .filter(Boolean)
                        .map((v) => <option key={v} value={v} />)}
                </datalist>
              </DetailRow>
              {versionInfo.recommendedTarget && targetVersion !== versionInfo.recommendedTarget ? (
                <DetailRow label="Recommended" value={versionInfo.recommendedTarget} />
              ) : null}
              <DetailRow label="Upgrade Supported">
                <SupportBadge supported={versionInfo.upgradeSupported} />
              </DetailRow>
              {versionInfo.reason ? (
                <DetailRow label="Reason" value={versionInfo.reason} />
              ) : null}
            </div>
          ) : (
            <p className="muted">Version information loads with cluster selection.</p>
          )}
        </InfoCard>

        <InfoCard title="Provider & Support">
          {provider ? (
            <div className="upgrade-details">
              <DetailRow label="Provider" value={provider.providerDisplay} />
              <DetailRow label="Execution Mode" value={provider.executionMode?.replace(/-/g, " ")} />
              <DetailRow label="Upgrade Supported">
                <SupportBadge supported={provider.upgradeSupported} />
              </DetailRow>
              <DetailRow label="Reason" value={provider.reason} />
            </div>
          ) : null}
        </InfoCard>

        <InfoCard title="Version Skew">
          {versionSkew ? (
            <div className="upgrade-details">
              <DetailRow label="Control Plane" value={versionSkew.controlPlaneVersion} />
              <div className="upgrade-detail-row upgrade-detail-row--stack">
                <span className="upgrade-detail-label">Nodes</span>
                <ul className="upgrade-node-list">
                  {(versionSkew.nodes || []).map((node) => (
                    <li key={node.name}>
                      <span>{node.name}</span>
                      <span className="muted">{node.version}</span>
                    </li>
                  ))}
                </ul>
              </div>
              <DetailRow label="Status">
                <span
                  className={`status-badge status-badge--${
                    versionSkew.status === "healthy" ? "pass" : "warning"
                  }`}
                >
                  {versionSkew.status === "healthy" ? "Healthy" : "Warning"}
                </span>
              </DetailRow>
              {(versionSkew.warnings || []).length ? (
                <div className="upgrade-warnings">
                  {(versionSkew.warnings || []).map((warning) => (
                    <p key={warning} className="upgrade-warning-text">
                      {warning}
                    </p>
                  ))}
                </div>
              ) : null}
            </div>
          ) : (
            <p className="muted">Version skew analysis available after cluster load.</p>
          )}
        </InfoCard>
      </section>

      <section className="content-grid upgrade-middle-grid">
        <InfoCard title="Precheck Results" actionLabel={canPrecheck ? "Run Precheck" : undefined} onAction={onRunPrecheck}>
          <ul className="check-list upgrade-check-list">
            {checks.length ? (
              checks.map((check) => (
                <li key={check.item} className={mapPrecheckClass(check.rawStatus || check.state)}>
                  <div className="upgrade-check-content">
                    <span>{check.item}</span>
                    {check.message ? <p className="upgrade-check-detail muted">{check.message}</p> : null}
                  </div>
                  <StatusBadge status={check.rawStatus || check.state} />
                </li>
              ))
            ) : (
              <li className="pending">
                <span>Run precheck to evaluate cluster readiness</span>
                <StatusBadge status="pending" />
              </li>
            )}
          </ul>
        </InfoCard>

        <InfoCard title="Upgrade Plan">
          {manualRequired ? (
            <p className="upgrade-manual-badge">Manual Upgrade Required</p>
          ) : null}
          <ol className="stepper upgrade-plan-stepper upgrade-plan-result">
            {stepsToRender.length ? (
              stepsToRender.map((step, index) => {
                const name = typeof step === "string" ? step : step.name;
                const stepStatus = step.status;
                const isActive = index <= (upgradeData?.activeStep ?? -1);
                return (
                  <li key={name} className={isActive ? "active" : ""}>
                    <span className="step-index">{step.step ?? index + 1}</span>
                    <div className="upgrade-step-content">
                      <span>{name}</span>
                      {step.message ? <p className="muted upgrade-step-message">{step.message}</p> : null}
                      {stepStatus === "manual" ? (
                        <span className="status-badge status-badge--warning">Manual</span>
                      ) : null}
                    </div>
                  </li>
                );
              })
            ) : (
              <li className="pending">
                <span className="step-index">1</span>
                <span>Load cluster info or run precheck to view the upgrade plan</span>
              </li>
            )}
          </ol>
        </InfoCard>
      </section>

      {instructions ? (
        <section className="card upgrade-instructions">
          <header>
            <h3>{instructions.title || "Upgrade Instructions"}</h3>
          </header>
          {instructions.summary ? <p>{instructions.summary}</p> : null}
          {instructions.steps?.length ? (
            <ol className="upgrade-instruction-steps">
              {instructions.steps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          ) : null}
        </section>
      ) : null}

      <section className="card upgrade-actions">
        {canPrecheck ? (
          <button type="button" className="btn-outline" onClick={onRunPrecheck} disabled={loading}>
            Run Precheck
          </button>
        ) : null}
        {canStart ? (
          <button
            type="button"
            className="primary"
            onClick={handlePrimaryAction}
            disabled={
              loading || upgradeRunning || (!showsInstructionsOnly && upgradeData?.canUpgrade === false)
            }
          >
            {actionLabel}
          </button>
        ) : null}
        {!executionSupported && upgradeData?.canUpgrade && manualRequired ? (
          <p className="muted upgrade-action-note">
            This provider does not support automated upgrades through KubeSight.
          </p>
        ) : null}
      </section>
    </AccessScopeView>
  );
}
