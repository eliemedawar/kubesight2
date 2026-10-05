import { useCallback, useEffect, useState } from "react";
import { getCiServiceSummary, listCiPipelines, updateCiService } from "../api/ciApi.js";
import { getCiAssistAvailability } from "../api/ciAssistApi.js";
import { useAuth } from "../context/AuthContext";
import { useRouteParam, useRouteQuery } from "../routes/RouterContext.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import ArtifactsPanel from "../components/catalog/ArtifactsPanel.jsx";
import BuildDetailDrawer from "../components/catalog/BuildDetailDrawer.jsx";
import BuildsPanel from "../components/catalog/BuildsPanel.jsx";
import DockerfilePanel from "../components/catalog/DockerfilePanel.jsx";
import HermesAnalysisPanel from "../components/catalog/HermesAnalysisPanel.jsx";
import IntelligencePanel from "../components/catalog/IntelligencePanel.jsx";
import MergeChecksPanel from "../components/catalog/MergeChecksPanel.jsx";
import PipelineEditor from "../components/catalog/PipelineEditor.jsx";
import RunBuildModal from "../components/catalog/RunBuildModal.jsx";
import ServiceFormModal from "../components/catalog/ServiceFormModal.jsx";
import ServiceOverview from "../components/catalog/ServiceOverview.jsx";
import ServiceSettingsPanel from "../components/catalog/ServiceSettingsPanel.jsx";
import SharedPipelineUsedBy from "../components/catalog/SharedPipelineUsedBy.jsx";
import SourcePanel from "../components/catalog/SourcePanel.jsx";
import { PlayIcon, StatusPill } from "../components/catalog/ciShared.jsx";

const TABS = [
  ["overview", "Overview"],
  ["source", "Source"],
  // Between Source and Pipeline because that is the order the questions come
  // in: where the code is, what it is, then how to build it.
  ["application", "Application"],
  // Application Intelligence reads the same repository the Source tab points
  // at, so it lives here rather than as a page that asks for it again.
  ["intelligence", "Intelligence"],
  ["pipeline", "Pipeline"],
  // Between Pipeline and Dockerfile because that is where it sits in the life
  // of a change: the pipeline is how this service is built, merge checks are
  // what a pull request has to survive before it becomes something to build.
  ["mergeChecks", "Merge Checks"],
  ["dockerfile", "Dockerfile"],
  ["builds", "Builds"],
  ["artifacts", "Artifacts"],
  ["settings", "Settings"],
];

// A pipeline from the Pipelines page is a service row of kind "pipeline", shown
// by this page with the tabs that mean something for it: no application, merge
// checks, Dockerfile or intelligence of its own. Its repository is optional.
const PIPELINE_TABS = [
  ["pipeline", "Pipeline"],
  ["builds", "Runs"],
  ["artifacts", "Artifacts"],
  ["repository", "Repository"],
  ["usedBy", "Used by"],
  ["settings", "Settings"],
];

/**
 * One service, ten tabs.
 *
 * Run Build lives in the header so it is reachable from every tab, and is
 * disabled with a reason when the service is not ready — never silently
 * clickable into a 400.
 */
export default function ServiceDetailPage({
  serviceId,
  initialTab,
  initialBuildId,
  onBack,
  onDeleted,
  variant = "service",
}) {
  const isPipeline = variant === "pipeline";
  const tabs = isPipeline ? PIPELINE_TABS : TABS;
  const { hasPermission } = useAuth();
  const can = {
    edit: hasPermission("ci_services:edit"),
    delete: hasPermission("ci_services:delete"),
    editPipeline: hasPermission("ci_pipelines:edit"),
    run: hasPermission("ci_builds:run"),
    cancel: hasPermission("ci_builds:cancel"),
    retry: hasPermission("ci_builds:retry"),
    viewSecrets: hasPermission("ci_secrets:view"),
    manageSecrets: hasPermission("ci_secrets:manage"),
    manageArtifacts: hasPermission("ci_artifacts:manage"),
    deploy: hasPermission("apps:deploy"),
    viewMergeChecks: hasPermission("ci_merge_checks:view"),
    manageMergeChecks: hasPermission("ci_merge_checks:manage"),
    viewIntelligence: hasPermission("applications:view"),
    manageIntelligence: hasPermission("applications:manage"),
    analyzeIntelligence: hasPermission("applications:analyze"),
  };

  const [summary, setSummary] = useState(null);
  const [stages, setStages] = useState([]);
  // Whether the assisted path may be offered at all. Asked once, here, so the
  // Application tab can show a reason instead of a control that fails.
  const [assist, setAssist] = useState(null);
  const [tab, changeTab] = useRouteParam("tab", initialTab || (isPipeline ? "pipeline" : "overview"));
  // The Pipeline, Merge Checks and Settings tabs are drafts until saved. Leaving one —
  // another tab, or back to the catalog — asks first, because the draft does
  // not survive the trip.
  const [pipelineDirty, setPipelineDirty] = useState(false);
  const [mergeChecksDirty, setMergeChecksDirty] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const draftLabel =
    tab === "pipeline" && pipelineDirty
      ? "pipeline"
      : tab === "mergeChecks" && mergeChecksDirty
        ? "merge check"
        : tab === "settings" && settingsDirty
          ? "settings"
          : null;
  const confirmLeaveDraft = () =>
    !draftLabel ||
    window.confirm(`Your ${draftLabel} changes are not saved. Leave and discard them?`);
  const setTab = (next) => {
    if (next === tab) return;
    if (!confirmLeaveDraft()) return;
    setPipelineDirty(false);
    setMergeChecksDirty(false);
    setSettingsDirty(false);
    changeTab(next);
  };
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [runOpen, setRunOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState("");
  // Replace, not push: the build drawer opens over the Builds tab rather
  // than being a place of its own, so Back leaves the service in one press.
  const [openBuildId, setOpenBuildId] = useRouteQuery("build", initialBuildId || null);
  // Bumped after a build is triggered so the Builds and Artifacts tabs reload.
  const [refreshToken, setRefreshToken] = useState(0);

  const load = useCallback(async () => {
    // Both requests leave together; the page waits for the slower one, not the sum.
    const pipelinesRequest = listCiPipelines(serviceId);
    pipelinesRequest.catch(() => {}); // awaited below; only silences the early-rejection warning
    try {
      const data = await getCiServiceSummary(serviceId);
      setSummary(data);
      setError("");
      const pipelines = await pipelinesRequest;
      setStages(pipelines.items?.[0]?.stages || []);
    } catch (err) {
      setError(err.message || "Could not load the service.");
    } finally {
      setLoading(false);
    }
  }, [serviceId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (isPipeline) return;
    getCiAssistAvailability()
      .then(setAssist)
      .catch(() =>
        setAssist({ available: false, reason: "Hermes could not be reached." })
      );
  }, [isPipeline]);

  const service = summary?.service;

  // The modal owns the trigger call; this only handles landing on the result.
  const buildStarted = (build) => {
    setRunOpen(false);
    setRefreshToken((value) => value + 1);
    setTab("builds");
    setOpenBuildId(String(build.id));
    load();
  };

  const saveIdentity = async (payload) => {
    setSavingEdit(true);
    setEditError("");
    try {
      await updateCiService(serviceId, payload);
      setEditing(false);
      load();
    } catch (err) {
      setEditError(err.message || "Could not save the service.");
    } finally {
      setSavingEdit(false);
    }
  };

  // Deploying hands the exact image reference to the existing deploy flow. CI
  // never applies anything to a cluster itself.
  const deployArtifact = (artifact) => {
    window.alert(
      `Deploy ${artifact.uri}\n\n` +
        "Container image builds land in Phase 4 (BuildKit + Nexus); this button " +
        "will then hand this exact reference to the existing deploy flow."
    );
  };

  if (loading) return <LoadingState label={isPipeline ? "Loading pipeline…" : "Loading service…"} />;
  if (!service || (service.kind === "pipeline") !== isPipeline) {
    return (
      <div className="ops-page">
        <ErrorBanner message={error || (isPipeline ? "Pipeline not found." : "Service not found.")} />
        <button type="button" className="btn-outline" onClick={onBack}>
          {isPipeline ? "Back to pipelines" : "Back to catalog"}
        </button>
      </div>
    );
  }

  const blockedReason = !summary.readiness.ready
    ? summary.readiness.checks.find((check) => !check.ok)?.hint
    : "";

  return (
    <div className="ops-page">
      <div className="sg-ph">
        <div>
          <button
            type="button"
            className="sg-ci-back"
            onClick={() => confirmLeaveDraft() && onBack()}
          >
            {isPipeline ? "← Pipelines" : "← CI Services"}
          </button>
          <h2>
            {service.name} <StatusPill status={service.status} />
            {isPipeline && <span className="chip sg-ci-kind-chip">Pipeline</span>}
          </h2>
          <p className="sg-ph-sub">
            {service.description || "No description."}
            {service.sourceConfigured &&
              ` · ${service.repositoryWorkspace}/${service.repositoryName} @ ${service.defaultBranch}`}
            {isPipeline && !service.sourceConfigured && " · No repository"}
            {isPipeline &&
              ` · ${service.usedByCount ? `used by ${service.usedByCount} service${service.usedByCount === 1 ? "" : "s"}` : "not used by a service"}`}
            {!isPipeline && service.sharedPipeline && ` · builds with the shared pipeline ${service.sharedPipeline.name}`}
          </p>
        </div>
        <div className="sg-ph-actions">
          {can.edit && (
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setEditError("");
                setEditing(true);
              }}
            >
              Edit
            </button>
          )}
          {can.run && (
            <button
              type="button"
              className="primary sg-cat-new"
              onClick={() => setRunOpen(true)}
              // A build runs the SAVED pipeline; starting one mid-edit would run
              // something other than what is on screen.
              disabled={Boolean(blockedReason) || pipelineDirty}
              title={
                pipelineDirty
                  ? "Save or discard your pipeline changes first — a build runs the saved pipeline"
                  : blockedReason ||
                    (isPipeline && !service.sourceConfigured ? "Run it now" : "Run a build — pick a branch or tag")
              }
            >
              <PlayIcon />
              {isPipeline ? "Run" : "Run build"}
            </button>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} />}

      <div className="tab-bar" role="tablist" aria-label={isPipeline ? "Pipeline sections" : "Service sections"}>
        {/* Merge checks is the one tab behind a permission of its own — it
            carries a webhook secret and the gate that decides what may be
            merged, so a role without that permission is not shown the door. */}
        {tabs.filter(
          ([value]) =>
            (value !== "mergeChecks" || can.viewMergeChecks) &&
            (value !== "intelligence" || can.viewIntelligence)
        ).map(([value, label]) => {
          const latest = summary.recentBuilds?.[0];
          const buildsAlert =
            value === "builds" && latest && ["failed", "timeout"].includes(latest.status);
          return (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={tab === value}
              className={tab === value ? "active" : ""}
              onClick={() => setTab(value)}
            >
              {label}
              {buildsAlert && <span className="sg-ci-tab-dot" aria-label="latest build failed" />}
            </button>
          );
        })}
      </div>

      <div className="sg-ci-tabpanel" role="tabpanel">
        {tab === "overview" && (
          <ServiceOverview
            summary={summary}
            stages={stages}
            onOpenBuild={setOpenBuildId}
            onGoToTab={setTab}
          />
        )}
        {(tab === "source" || (isPipeline && tab === "repository")) && (
          <SourcePanel
            service={service}
            canEdit={can.edit}
            canManageSecrets={can.manageSecrets}
            onSaved={() => load()}
          />
        )}
        {tab === "application" && (
          <HermesAnalysisPanel
            service={service}
            availability={assist}
            canEdit={can.editPipeline}
            onAccepted={() => {
              // The proposal is now an ordinary pipeline. Land on it, so what
              // was approved is the first thing seen afterwards.
              setTab("pipeline");
              load();
            }}
            onConfigureManually={() => setTab("pipeline")}
          />
        )}
        {tab === "intelligence" && can.viewIntelligence && (
          <IntelligencePanel
            service={service}
            canManage={can.manageIntelligence}
            canAnalyze={can.analyzeIntelligence}
            onGoToTab={setTab}
          />
        )}
        {tab === "pipeline" && (
          <PipelineEditor
            service={service}
            canEdit={can.editPipeline}
            onChanged={load}
            onDirtyChange={setPipelineDirty}
            onGoToTab={setTab}
          />
        )}
        {tab === "mergeChecks" && (
          <MergeChecksPanel
            service={service}
            canEdit={can.manageMergeChecks}
            canView={can.viewMergeChecks}
            onDirtyChange={setMergeChecksDirty}
            onGoToTab={setTab}
            onOpenBuild={setOpenBuildId}
          />
        )}
        {tab === "dockerfile" && (
          <DockerfilePanel service={service} canEdit={can.edit} onSaved={() => load()} />
        )}
        {tab === "builds" && (
          <BuildsPanel
            service={service}
            canCancel={can.cancel}
            canRetry={can.retry}
            refreshToken={refreshToken}
          />
        )}
        {tab === "artifacts" && (
          <ArtifactsPanel
            service={service}
            canDeploy={can.deploy}
            canManage={can.manageArtifacts}
            onDeploy={deployArtifact}
            refreshToken={refreshToken}
          />
        )}
        {isPipeline && tab === "usedBy" && (
          <SharedPipelineUsedBy pipeline={service} canAttach={can.editPipeline} onChanged={load} />
        )}
        {tab === "settings" && (
          <ServiceSettingsPanel
            service={service}
            expectedSecrets={summary.expectedSecrets || []}
            canEdit={can.edit}
            canDelete={can.delete}
            canViewSecrets={can.viewSecrets}
            canManageSecrets={can.manageSecrets}
            canViewSchedules={hasPermission("ci_builds:view")}
            canEditSchedules={can.editPipeline && can.run}
            canRunSchedules={can.run}
            canDeploy={can.deploy}
            onOpenBuild={(id) => setOpenBuildId(String(id))}
            onSaved={() => load()}
            onDeleted={onDeleted}
            onDirtyChange={setSettingsDirty}
          />
        )}
      </div>

      {editing && (
        <ServiceFormModal
          service={service}
          onClose={() => setEditing(false)}
          onSave={saveIdentity}
          saving={savingEdit}
          error={editError}
        />
      )}

      {runOpen && (
        <RunBuildModal
          service={service}
          onClose={() => setRunOpen(false)}
          onStarted={buildStarted}
        />
      )}

      {openBuildId && (
        <BuildDetailDrawer
          buildId={openBuildId}
          onClose={() => setOpenBuildId(null)}
          onChanged={() => {
            setRefreshToken((value) => value + 1);
            load();
          }}
          canCancel={can.cancel}
          canRetry={can.retry}
        />
      )}
    </div>
  );
}
