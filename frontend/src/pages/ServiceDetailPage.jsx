import { useCallback, useEffect, useState } from "react";
import { getCiServiceSummary, listCiPipelines, updateCiService } from "../api/ciApi.js";
import { useAuth } from "../context/AuthContext";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import ArtifactsPanel from "../components/catalog/ArtifactsPanel.jsx";
import BuildDetailDrawer from "../components/catalog/BuildDetailDrawer.jsx";
import BuildsPanel from "../components/catalog/BuildsPanel.jsx";
import PipelineEditor from "../components/catalog/PipelineEditor.jsx";
import RunBuildModal from "../components/catalog/RunBuildModal.jsx";
import ServiceFormModal from "../components/catalog/ServiceFormModal.jsx";
import ServiceOverview from "../components/catalog/ServiceOverview.jsx";
import ServiceSettingsPanel from "../components/catalog/ServiceSettingsPanel.jsx";
import SourcePanel from "../components/catalog/SourcePanel.jsx";
import { PlayIcon, StatusPill } from "../components/catalog/ciShared.jsx";

const TABS = [
  ["overview", "Overview"],
  ["source", "Source"],
  ["pipeline", "Pipeline"],
  ["builds", "Builds"],
  ["artifacts", "Artifacts"],
  ["settings", "Settings"],
];

/**
 * One service, six tabs.
 *
 * Run Build lives in the header so it is reachable from every tab, and is
 * disabled with a reason when the service is not ready — never silently
 * clickable into a 400.
 */
export default function ServiceDetailPage({ serviceId, initialTab, initialBuildId, onBack, onDeleted }) {
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
    deploy: hasPermission("apps:deploy"),
  };

  const [summary, setSummary] = useState(null);
  const [stages, setStages] = useState([]);
  const [tab, setTab] = useState(initialTab || "overview");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [runOpen, setRunOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState("");
  const [openBuildId, setOpenBuildId] = useState(initialBuildId || null);
  // Bumped after a build is triggered so the Builds and Artifacts tabs reload.
  const [refreshToken, setRefreshToken] = useState(0);

  const load = useCallback(async () => {
    try {
      const data = await getCiServiceSummary(serviceId);
      setSummary(data);
      setError("");
      const pipelines = await listCiPipelines(serviceId);
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

  const service = summary?.service;

  // The modal owns the trigger call; this only handles landing on the result.
  const buildStarted = (build) => {
    setRunOpen(false);
    setRefreshToken((value) => value + 1);
    setTab("builds");
    setOpenBuildId(build.id);
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

  if (loading) return <LoadingState label="Loading service…" />;
  if (!service) {
    return (
      <div className="ops-page">
        <ErrorBanner message={error || "Service not found."} />
        <button type="button" className="btn-outline" onClick={onBack}>
          Back to catalog
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
          <button type="button" className="sg-ci-back" onClick={onBack}>
            ← Service Catalog
          </button>
          <h2>
            {service.name} <StatusPill status={service.status} />
          </h2>
          <p className="sg-ph-sub">
            {service.description || "No description."}
            {service.sourceConfigured &&
              ` · ${service.repositoryWorkspace}/${service.repositoryName} @ ${service.defaultBranch}`}
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
              disabled={Boolean(blockedReason)}
              title={blockedReason || "Run a build — pick a branch or tag"}
            >
              <PlayIcon />
              Run build
            </button>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} />}

      <div className="tab-bar" role="tablist" aria-label="Service sections">
        {TABS.map(([value, label]) => {
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
        {tab === "source" && (
          <SourcePanel
            service={service}
            canEdit={can.edit}
            canManageSecrets={can.manageSecrets}
            onSaved={() => load()}
          />
        )}
        {tab === "pipeline" && (
          <PipelineEditor
            service={service}
            canEdit={can.editPipeline}
            onChanged={load}
          />
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
            onDeploy={deployArtifact}
            refreshToken={refreshToken}
          />
        )}
        {tab === "settings" && (
          <ServiceSettingsPanel
            service={service}
            canEdit={can.edit}
            canDelete={can.delete}
            canViewSecrets={can.viewSecrets}
            canManageSecrets={can.manageSecrets}
            onSaved={() => load()}
            onDeleted={onDeleted}
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
