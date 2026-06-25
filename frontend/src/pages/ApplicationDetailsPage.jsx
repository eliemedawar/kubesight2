import { useCallback, useEffect, useMemo, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import InfoCard from "../components/common/InfoCard.jsx";
import HelmReleasePanel from "../components/inventory/HelmReleasePanel.jsx";
import ConfirmActionModal from "../components/inventory/ConfirmActionModal.jsx";
import DataTable from "../components/common/DataTable.jsx";
import ResourceInspectModal from "../components/resources/ResourceInspectModal.jsx";
import { getLogs, getNamespaceEvents, getResourceDescribe } from "../api/clustersApi.js";
import { formatAccessError } from "../utils/authz.js";
import { formatLogLinesToLocalTime } from "../utils/logFormat.js";
import SearchableSelect from "../components/common/SearchableSelect.jsx";
import {
  compareApplicationVersions,
  getApplicationVersion,
  getRolloutHistory,
  listApplicationVersions,
  restartWorkload,
  rollbackApplicationVersion,
  rollbackWorkload,
  scaleWorkload,
} from "../api/inventoryApi.js";
import ApplicationBuilderWizard from "../components/inventory/wizard/ApplicationBuilderWizard.jsx";
import YamlPreviewPanel from "../components/inventory/wizard/YamlPreviewPanel.jsx";
import { createEmptyWizardState } from "../components/inventory/wizard/wizardDefaults.js";
import {
  buildWorkloadActionPayload,
  canOperateDeployment,
  getPrimaryWorkloadName,
  hasHelmRelease,
  inventoryIdsMatch,
} from "../utils/inventoryPermissions.js";

const CLOSED_INSPECT_MODAL = {
  open: false,
  title: "",
  mode: "text",
  loading: false,
  error: "",
  content: "",
  rolloutRows: [],
};

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "pods", label: "Pods" },
  { key: "resources", label: "Resources" },
  { key: "versions", label: "Versions" },
  { key: "yaml", label: "YAML" },
  { key: "logs", label: "Logs" },
  { key: "events", label: "Events" },
  { key: "helm", label: "Helm" },
  { key: "actions", label: "Actions" },
];

function DetailRow({ label, value }) {
  return (
    <div className="upgrade-detail-row">
      <span className="upgrade-detail-label">{label}</span>
      <span className="upgrade-detail-value">{value ?? "—"}</span>
    </div>
  );
}

function MetadataItem({ label, value }) {
  const text = value == null || value === "" ? "—" : String(value);
  return (
    <div className="app-details-metadata-item">
      <dt>{label}</dt>
      <dd title={text !== "—" ? text : undefined}>{text}</dd>
    </div>
  );
}

function formatReplicasSummary(summary) {
  const ready = summary?.readyReplicas ?? "—";
  const desired = summary?.replicas ?? "—";
  return `${ready}/${desired}`;
}

export default function ApplicationDetailsPage({
  detail,
  selectedApplicationId = "",
  loading,
  onBack,
  user,
  activeTab = "overview",
  onTabChange,
  canViewLogs,
  canUpdateCatalog = false,
  canRemoveFromInventory = false,
  canDeploy = false,
  canViewHelm = false,
  canUpgradeHelm = false,
  canRollbackHelm = false,
  canUninstallHelm = false,
  onEditCatalog,
  onRemoveFromInventory,
  onDeployUpdate,
  onHelmUpgrade,
  onHelmActionComplete,
  onRefreshDetail,
  clusterOptions = [],
}) {
  const [tab, setTab] = useState(activeTab);
  const [logPod, setLogPod] = useState(null);
  const [logLines, setLogLines] = useState([]);
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError] = useState("");
  const [events, setEvents] = useState([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState("");
  const [eventsUnavailable, setEventsUnavailable] = useState(false);
  const [rolloutHistory, setRolloutHistory] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [actionModal, setActionModal] = useState(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [scaleReplicas, setScaleReplicas] = useState("");
  const [rollbackRevision, setRollbackRevision] = useState("");
  const [inspectModal, setInspectModal] = useState(CLOSED_INSPECT_MODAL);
  const [deployWizardOpen, setDeployWizardOpen] = useState(false);
  const [versions, setVersions] = useState([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState("");
  const [selectedVersion, setSelectedVersion] = useState(null);
  const [compareVersionB, setCompareVersionB] = useState("");
  const [versionDiff, setVersionDiff] = useState("");
  const [versionYaml, setVersionYaml] = useState("");
  const [rollbackConfirm, setRollbackConfirm] = useState("");
  const [versionActionBusy, setVersionActionBusy] = useState(false);
  const [versionActionError, setVersionActionError] = useState("");

  const closeInspectModal = useCallback(() => {
    setInspectModal(CLOSED_INSPECT_MODAL);
  }, []);

  const openDescribePod = useCallback(async (pod) => {
    const summaryData = detail?.summary || {};
    const clusterId = summaryData.cluster || summaryData.clusterId;
    const namespace = summaryData.namespace;
    const podName = pod?.name;
    if (!clusterId || !namespace || !podName) {
      return;
    }
    setInspectModal({
      ...CLOSED_INSPECT_MODAL,
      open: true,
      title: `Describe pod / ${podName}`,
      mode: "describe",
      loading: true,
    });
    try {
      const payload = await getResourceDescribe({
        clusterId,
        namespace,
        kind: "pod",
        name: podName,
      });
      setInspectModal((prev) => ({
        ...prev,
        loading: false,
        content: payload.output || payload.yaml || "",
      }));
    } catch (err) {
      setInspectModal((prev) => ({
        ...prev,
        loading: false,
        error: formatAccessError(err.message) || err.message || "Request failed",
      }));
    }
  }, [detail]);

  useEffect(() => {
    setTab(activeTab);
  }, [activeTab, detail?.id]);

  useEffect(() => {
    setLogPod(null);
    setLogLines([]);
    setLogError("");
  }, [detail?.id]);

  const summary = detail?.summary || {};
  const catalog = detail?.catalog || {};
  const metrics = detail?.metrics || {};
  const hasCatalog = Boolean(summary.catalogEntryId || catalog.id);
  const workloadName = getPrimaryWorkloadName({ ...summary, workloadNames: detail?.workloads?.map((w) => w.name) });
  const primaryWorkload = workloadName || summary.applicationName;
  const canDeployOps =
    canDeploy &&
    user &&
    detail &&
    canOperateDeployment(user, {
      cluster: summary.cluster,
      clusterId: summary.cluster,
      namespace: summary.namespace,
      workloadType: summary.type || "Deployment",
      workloadNames: [primaryWorkload],
    });
  const showHelmTab =
    summary.source === "Helm" || detail?.helm || summary.releaseName || hasHelmRelease({ releaseName: summary.releaseName, source: summary.source });

  const visibleTabs = useMemo(
    () => TABS.filter((t) => (t.key === "helm" ? showHelmTab : true)),
    [showHelmTab]
  );

  const loggablePods = useMemo(
    () => (detail?.pods || []).filter((pod) => pod.canViewLogs !== false),
    [detail?.pods]
  );

  const changeTab = (key) => {
    setTab(key);
    onTabChange?.(key);
  };

  const viewLogs = async (pod, { switchTab = false } = {}) => {
    if (!canViewLogs || pod.canViewLogs === false) return;
    setLogPod(pod.name);
    setLogLoading(true);
    setLogError("");
    try {
      const result = await getLogs({
        cluster: summary.cluster,
        namespace: summary.namespace,
        pod: pod.name,
      });
      setLogLines(formatLogLinesToLocalTime(result.lines || []));
      if (switchTab) changeTab("logs");
    } catch (err) {
      setLogError(err.message || "Failed to load logs");
      setLogLines([]);
    } finally {
      setLogLoading(false);
    }
  };

  const handleLogPodSelect = (event) => {
    const podName = event.target.value;
    if (!podName) {
      setLogPod(null);
      setLogLines([]);
      setLogError("");
      return;
    }
    const pod = loggablePods.find((p) => p.name === podName);
    if (pod) viewLogs(pod);
  };

  useEffect(() => {
    if (!detail || tab !== "events") return;
    const clusterId = summary.cluster;
    const namespace = summary.namespace;
    if (!clusterId || !namespace) return;

    let cancelled = false;
    setEventsLoading(true);
    setEventsError("");
    setEventsUnavailable(false);

    const involvedKind = summary.type === "Deployment" ? "Deployment" : "Pod";
    const involvedName = primaryWorkload;

    getNamespaceEvents(clusterId, namespace, {
      involvedKind,
      involvedName,
      limit: 50,
    })
      .then((payload) => {
        if (cancelled) return;
        const items = payload?.items || (Array.isArray(payload) ? payload : []);
        setEvents(items);
      })
      .catch((err) => {
        if (cancelled) return;
        const msg = String(err.message || "");
        if (msg.includes("404") || msg.toLowerCase().includes("not found")) {
          setEventsUnavailable(true);
          setEvents(detail.events || []);
        } else {
          setEventsError(msg || "Could not load events.");
          setEvents(detail.events || []);
        }
      })
      .finally(() => {
        if (!cancelled) setEventsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [detail, tab, summary.cluster, summary.namespace, summary.type, primaryWorkload]);

  useEffect(() => {
    if (!detail || tab !== "versions") return;
    const inventoryId = detail.id;
    if (!inventoryId) return;
    let cancelled = false;
    setVersionsLoading(true);
    setVersionsError("");
    listApplicationVersions(inventoryId)
      .then((rows) => {
        if (!cancelled) setVersions(rows || []);
      })
      .catch((err) => {
        if (!cancelled) setVersionsError(err.message || "Failed to load versions");
      })
      .finally(() => {
        if (!cancelled) setVersionsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [detail, tab]);

  useEffect(() => {
    if (!selectedVersion) {
      setVersionYaml("");
      return;
    }
    getApplicationVersion(selectedVersion.id, true)
      .then((v) => setVersionYaml(v.yaml || ""))
      .catch(() => setVersionYaml(""));
  }, [selectedVersion]);

  useEffect(() => {
    if (!detail || tab !== "actions" || !canDeployOps) return;
    const clusterId = summary.cluster;
    const namespace = summary.namespace;
    if (!clusterId || !namespace || !primaryWorkload) return;

    let cancelled = false;
    setHistoryLoading(true);
    setHistoryError("");
    getRolloutHistory({ clusterId, namespace, workloadName: primaryWorkload })
      .then((data) => {
        if (!cancelled) setRolloutHistory(data);
      })
      .catch((err) => {
        if (!cancelled) setHistoryError(err.message || "Rollout history unavailable");
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [detail, tab, canDeployOps, summary.cluster, summary.namespace, primaryWorkload]);

  const actionItem = detail
    ? {
        cluster: summary.cluster,
        clusterId: summary.cluster,
        namespace: summary.namespace,
        workloadType: "Deployment",
        workloadName: primaryWorkload,
        replicas: summary.replicas,
        readyReplicas: summary.readyReplicas,
      }
    : null;

  const closeActionModal = () => {
    if (actionBusy) return;
    setActionModal(null);
    setActionError("");
    setRollbackRevision("");
  };

  const runDeploymentAction = async () => {
    if (!actionModal || !actionItem) return;
    setActionBusy(true);
    setActionError("");
    try {
      const payload = buildWorkloadActionPayload(actionItem);
      if (actionModal === "restart") {
        await restartWorkload(payload);
      } else if (actionModal === "scale") {
        const replicas = Number(scaleReplicas);
        if (!Number.isFinite(replicas) || replicas < 0 || replicas > 50) {
          setActionError("Replicas must be between 0 and 50.");
          setActionBusy(false);
          return;
        }
        await scaleWorkload({ ...payload, replicas });
      } else if (actionModal === "rollback") {
        const body = { ...payload };
        if (rollbackRevision.trim()) {
          body.revision = Number(rollbackRevision);
        }
        await rollbackWorkload(body);
      }
      closeActionModal();
      onRefreshDetail?.();
      onHelmActionComplete?.();
    } catch (err) {
      setActionError(err.message || "Action failed");
    } finally {
      setActionBusy(false);
    }
  };

  const detailMatchesSelection =
    !selectedApplicationId || inventoryIdsMatch(detail?.id, selectedApplicationId);

  if (loading || (selectedApplicationId && !detailMatchesSelection)) {
    return <p className="muted">Loading application details…</p>;
  }

  if (!detail) {
    return (
      <>
        <PageTitle title="Application Details" subtitle="Workload drill-down" />
        <EmptyState message="Application not found or you may not have access to this namespace." />
        <button type="button" className="btn-outline" onClick={onBack}>
          Back to Inventory
        </button>
      </>
    );
  }

  const lastDeployment =
    detail.helm?.lastDeployed || summary.lastDeployed || summary.lastUpdated || summary.creationTime;
  const helmReleaseLabel =
    summary.releaseName || detail.helm?.releaseName || detail.helm?.chart || (summary.source === "Helm" ? "Linked" : "—");
  const tags = summary.tags || catalog.tags || [];
  const cpuDisplay = metrics.aggregate?.cpu || summary.cpuUsage || "—";
  const memoryDisplay = metrics.aggregate?.memory || summary.memoryUsage || "—";

  return (
    <div className="ops-page app-details-page">
      <PageTitle
        title={summary.applicationName || "Application"}
        subtitle={`${summary.namespace} · ${summary.clusterName || summary.cluster} · ${summary.status || "Unknown"}`}
        actionLabel="Back to Inventory"
        onAction={onBack}
      />

      <nav className="app-details-tabs" aria-label="Application sections">
        {visibleTabs.map((t) => (
          <button
            key={t.key}
            type="button"
            className={tab === t.key ? "app-details-tabs__btn is-active" : "app-details-tabs__btn"}
            onClick={() => changeTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {(canUpdateCatalog || canRemoveFromInventory || canDeploy) && hasCatalog ? (
        <div className="inventory-detail-actions">
          {canUpdateCatalog ? (
            <button type="button" className="btn-outline" onClick={() => onEditCatalog?.(detail)}>
              Edit Metadata
            </button>
          ) : null}
          {canRemoveFromInventory ? (
            <button type="button" className="btn-outline btn-danger-outline" onClick={() => onRemoveFromInventory?.(detail)}>
              Remove from Inventory
            </button>
          ) : null}
          {canDeploy ? (
            <button type="button" className="btn-primary" onClick={() => setDeployWizardOpen(true)}>
              Deploy Update
            </button>
          ) : null}
        </div>
      ) : null}

      {tab === "overview" ? (
        <section className="app-details-panel">
          <div className="app-details-overview-grid">
            <InfoCard title="Operational summary">
              <DetailRow label="Status" value={summary.status} />
              <DetailRow label="Namespace" value={summary.namespace} />
              <DetailRow label="Workload type" value={summary.type} />
              <DetailRow label="Workload name" value={primaryWorkload} />
              <DetailRow label="Version / image" value={summary.version || summary.image} />
              <DetailRow label="Replicas (ready/desired)" value={formatReplicasSummary(summary)} />
            </InfoCard>

            <InfoCard title="Infrastructure & resources">
              <dl className="app-details-metadata-grid">
                <MetadataItem label="Cluster" value={summary.clusterName || summary.cluster} />
                <MetadataItem label="CPU" value={cpuDisplay} />
                <MetadataItem label="Memory" value={memoryDisplay} />
                <MetadataItem label="Helm release" value={helmReleaseLabel} />
                <MetadataItem label="Last deployment" value={lastDeployment} />
                <MetadataItem label="Source" value={summary.source || "Discovered"} />
              </dl>
            </InfoCard>

            <InfoCard title="Ownership & catalog">
              <dl className="app-details-metadata-grid">
                <MetadataItem label="Owner team" value={summary.ownerTeam || catalog.ownerTeam || "Unassigned"} />
                <MetadataItem label="Criticality" value={summary.criticality || catalog.criticality || "Not set"} />
                <MetadataItem label="Environment" value={summary.environment || catalog.environment || "Not set"} />
                <MetadataItem label="Contact" value={summary.contactEmail || catalog.contactEmail || "Not set"} />
                <MetadataItem label="Tags" value={tags.length ? tags.join(", ") : "Not set"} />
                <MetadataItem label="Catalog entry" value={hasCatalog ? "Registered" : "Discovered only"} />
              </dl>
              {catalog.description || summary.description ? (
                <DetailRow label="Description" value={catalog.description || summary.description} />
              ) : null}
              {catalog.documentationUrl || summary.documentationUrl ? (
                <DetailRow label="Documentation" value={catalog.documentationUrl || summary.documentationUrl} />
              ) : null}
            </InfoCard>
          </div>

          <div className="app-details-overview-grid">
            <InfoCard title="Workloads">
              <DataTable
                columns={[
                  { key: "name", label: "Name" },
                  { key: "type", label: "Type" },
                  { key: "ready", label: "Ready" },
                  { key: "desired", label: "Desired" },
                  { key: "image", label: "Image" },
                  { key: "age", label: "Age" },
                ]}
                rows={(detail.workloads || []).map((w) => ({
                  ...w,
                  ready: w.ready ?? "—",
                  desired: w.desired ?? "—",
                }))}
              />
              {!detail.workloads?.length ? <p className="muted">No workloads linked.</p> : null}
            </InfoCard>

            <InfoCard title="Services & ingress">
              {(detail.services || []).length ? (
                <DataTable
                  columns={[
                    { key: "name", label: "Service" },
                    { key: "type", label: "Type" },
                    { key: "ports", label: "Ports" },
                  ]}
                  rows={(detail.services || []).map((svc) => ({
                    ...svc,
                    ports: Array.isArray(svc.ports) ? svc.ports.join(", ") : svc.ports || "—",
                  }))}
                />
              ) : (
                <p className="muted">No services linked to this application.</p>
              )}
              {(detail.ingress || []).length ? (
                <DataTable
                  columns={[
                    { key: "name", label: "Ingress" },
                    { key: "host", label: "Host" },
                    { key: "path", label: "Path" },
                  ]}
                  rows={detail.ingress}
                />
              ) : null}
            </InfoCard>
          </div>
        </section>
      ) : null}

      {tab === "pods" ? (
        <div className="app-details-panel">
        <InfoCard title="Pods">
          <DataTable
            columns={[
              { key: "name", label: "Pod" },
              { key: "status", label: "Status" },
              { key: "restarts", label: "Restarts" },
              { key: "node", label: "Node" },
              { key: "age", label: "Age" },
              { key: "cpuUsage", label: "CPU" },
              { key: "memoryUsage", label: "Memory" },
              { key: "actions", label: "Actions" },
            ]}
            rows={(detail.pods || []).map((pod) => ({
              ...pod,
              node: pod.node || "—",
              age: pod.age || "—",
              cpuUsage: pod.cpuUsage || "—",
              memoryUsage: pod.memoryUsage || "—",
              restarts: pod.restarts ?? "—",
              actions: (
                <div className="inventory-actions-cell">
                  {canViewLogs && pod.canViewLogs !== false ? (
                    <button type="button" className="btn-outline btn-sm" onClick={() => viewLogs(pod, { switchTab: true })}>
                      Logs
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="btn-outline btn-sm"
                    onClick={() => openDescribePod(pod)}
                  >
                    Describe
                  </button>
                </div>
              ),
            }))}
          />
          {!detail.pods?.length ? <p className="muted">No pods found for this application.</p> : null}
        </InfoCard>
        </div>
      ) : null}

      {tab === "logs" ? (
        <div className="app-details-panel">
        <InfoCard title="Logs">
          {!canViewLogs ? (
            <p className="muted">You do not have permission to view logs for this application.</p>
          ) : (
            <>
              <label className="app-logs-pod-select">
                <span className="app-logs-pod-select__label">Pod</span>
                <SearchableSelect
                  className="app-logs-pod-select__control"
                  value={logPod || ""}
                  onChange={handleLogPodSelect}
                  disabled={logLoading || !loggablePods.length}
                  aria-label="Select a pod to stream recent log lines"
                >
                  <option value="">Select a pod…</option>
                  {loggablePods.map((pod) => (
                    <option key={pod.name} value={pod.name}>
                      {pod.name}
                      {pod.status ? ` · ${pod.status}` : ""}
                    </option>
                  ))}
                </SearchableSelect>
              </label>
              {!loggablePods.length ? (
                <p className="muted">No pods available for log viewing in this application.</p>
              ) : null}
              {logPod ? (
                <div className="inventory-logs-panel">
                  <h4 className="eyebrow">Logs — {logPod}</h4>
                  {logLoading ? <p className="muted">Loading logs…</p> : null}
                  {logError ? <p className="banner-message error">{logError}</p> : null}
                  <pre className="logs-output">{logLines.join("\n") || "(empty)"}</pre>
                </div>
              ) : loggablePods.length ? (
                <EmptyState message="Choose a pod from the dropdown to load logs." />
              ) : null}
            </>
          )}
        </InfoCard>
        </div>
      ) : null}

      {tab === "events" ? (
        <div className="app-details-panel">
        <InfoCard title="Events">
          {eventsLoading ? <p className="muted">Loading events…</p> : null}
          {eventsUnavailable ? (
            <p className="muted">Live events API is not available in this environment. Showing cached events when present.</p>
          ) : null}
          {eventsError ? <p className="banner-message error">{eventsError}</p> : null}
          {(events || []).length ? (
            <DataTable
              columns={[
                { key: "type", label: "Type" },
                { key: "reason", label: "Reason" },
                { key: "message", label: "Message" },
                { key: "involvedObject", label: "Involved" },
                { key: "count", label: "Count" },
                { key: "time", label: "Time" },
              ]}
              rows={events.map((event, index) => ({
                id: `${event.reason}-${index}`,
                type: event.type || "—",
                reason: event.reason || "—",
                message: event.message || "—",
                involvedObject: [event.involvedKind, event.involvedName].filter(Boolean).join("/") || "—",
                count: event.count ?? "—",
                time: event.lastTimestamp || event.firstTimestamp || event.time || event.age || "—",
              }))}
            />
          ) : !eventsLoading ? (
            <EmptyState message="No recent events for this workload. Events appear when Kubernetes reports scheduling, scaling, or health changes." />
          ) : null}
        </InfoCard>
        </div>
      ) : null}

      {tab === "resources" ? (
        <div className="app-details-panel">
          <div className="app-details-overview-grid">
            <InfoCard title="Services">
              <DataTable
                columns={[
                  { key: "name", label: "Name" },
                  { key: "type", label: "Type" },
                  { key: "ports", label: "Ports" },
                ]}
                rows={(detail.services || []).map((svc) => ({
                  ...svc,
                  ports: Array.isArray(svc.ports) ? svc.ports.join(", ") : svc.ports || "—",
                }))}
              />
              {!detail.services?.length ? <p className="muted">No services.</p> : null}
            </InfoCard>
            <InfoCard title="Ingress">
              <DataTable
                columns={[
                  { key: "name", label: "Name" },
                  { key: "host", label: "Host" },
                  { key: "path", label: "Path" },
                ]}
                rows={detail.ingress || []}
              />
              {!detail.ingress?.length ? <p className="muted">No ingress resources.</p> : null}
            </InfoCard>
            <InfoCard title="ConfigMaps">
              <DataTable columns={[{ key: "name", label: "Name" }]} rows={detail.configMaps || []} />
              {!detail.configMaps?.length ? <p className="muted">No config maps linked.</p> : null}
            </InfoCard>
            <InfoCard title="Secrets">
              <DataTable columns={[{ key: "name", label: "Name" }]} rows={detail.secrets || []} />
              {!detail.secrets?.length ? <p className="muted">No secrets linked.</p> : null}
            </InfoCard>
          </div>
        </div>
      ) : null}

      {tab === "versions" ? (
        <div className="app-details-panel">
          <InfoCard title="Deployment version history">
            {versionsLoading ? <p className="muted">Loading versions…</p> : null}
            {versionsError ? <p className="banner-message error">{versionsError}</p> : null}
            {versions.length ? (
              <DataTable
                columns={[
                  { key: "versionLabel", label: "Version" },
                  { key: "workloadType", label: "Type" },
                  { key: "changeSummary", label: "Change summary" },
                  { key: "createdBy", label: "User" },
                  { key: "createdAt", label: "Timestamp" },
                  { key: "actions", label: "Actions" },
                ]}
                rows={versions.map((v) => ({
                  ...v,
                  createdAt: v.createdAt ? new Date(v.createdAt).toLocaleString() : "—",
                  actions: (
                    <div className="inventory-actions-cell">
                      <button type="button" className="btn-outline btn-sm" onClick={() => setSelectedVersion(v)}>
                        View
                      </button>
                      {canDeploy ? (
                        <button
                          type="button"
                          className="btn-outline btn-sm"
                          onClick={() => {
                            setSelectedVersion(v);
                            setRollbackConfirm("");
                          }}
                        >
                          Rollback
                        </button>
                      ) : null}
                    </div>
                  ),
                }))}
              />
            ) : !versionsLoading ? (
              <EmptyState message="No deployment versions recorded yet. Deploy via Application Builder to start version history." />
            ) : null}
            {selectedVersion ? (
              <div className="wizard-version-detail card">
                <h4>{selectedVersion.versionLabel}</h4>
                <p className="muted">{selectedVersion.changeSummary}</p>
                <YamlPreviewPanel yaml={versionYaml} readOnly />
                {canDeploy ? (
                  <>
                    <label className="wizard-field">
                      <span className="wizard-field__label">Compare with</span>
                      <SearchableSelect value={compareVersionB} onChange={(e) => setCompareVersionB(e.target.value)}>
                        <option value="">Select version…</option>
                        {versions.filter((v) => v.id !== selectedVersion.id).map((v) => (
                          <option key={v.id} value={v.id}>{v.versionLabel}</option>
                        ))}
                      </SearchableSelect>
                    </label>
                    <button
                      type="button"
                      className="btn-outline btn-sm"
                      disabled={!compareVersionB}
                      onClick={async () => {
                        const result = await compareApplicationVersions(selectedVersion.id, Number(compareVersionB));
                        setVersionDiff(result.diff || "");
                      }}
                    >
                      Compare versions
                    </button>
                    {versionDiff ? <pre className="wizard-yaml-panel__diff">{versionDiff}</pre> : null}
                    <label className="wizard-field">
                      <span className="wizard-field__label">Rollback — type APPLY {summary.namespace}</span>
                      <input value={rollbackConfirm} onChange={(e) => setRollbackConfirm(e.target.value)} />
                    </label>
                    <button
                      type="button"
                      className="btn-primary btn-sm"
                      disabled={versionActionBusy || rollbackConfirm !== `APPLY ${summary.namespace}`}
                      onClick={async () => {
                        setVersionActionBusy(true);
                        setVersionActionError("");
                        try {
                          await rollbackApplicationVersion(selectedVersion.id, rollbackConfirm);
                          onRefreshDetail?.();
                          setRollbackConfirm("");
                        } catch (err) {
                          setVersionActionError(err.message || "Rollback failed");
                        } finally {
                          setVersionActionBusy(false);
                        }
                      }}
                    >
                      Confirm rollback
                    </button>
                    {versionActionError ? <p className="banner-message error">{versionActionError}</p> : null}
                  </>
                ) : null}
              </div>
            ) : null}
          </InfoCard>
        </div>
      ) : null}

      {tab === "yaml" ? (
        <div className="app-details-panel">
          <InfoCard title="Current manifest">
            {selectedVersion?.yaml || versionYaml ? (
              <YamlPreviewPanel yaml={versionYaml || selectedVersion?.yaml || ""} readOnly />
            ) : versions[0] ? (
              <p className="muted">Select a version in the Versions tab to view YAML snapshots.</p>
            ) : (
              <EmptyState message="No stored YAML manifest. Deploy via Application Builder to capture manifests." />
            )}
          </InfoCard>
        </div>
      ) : null}

      {tab === "helm" && showHelmTab ? (
        <div className="app-details-panel">
          <HelmReleasePanel
            helm={detail.helm}
            summary={summary}
            canViewHelm={canViewHelm}
            canUpgradeHelm={canUpgradeHelm}
            canRollbackHelm={canRollbackHelm}
            canUninstallHelm={canUninstallHelm}
            onUpgrade={() => onHelmUpgrade?.(detail)}
            onViewManifest={() => {}}
            onActionComplete={onHelmActionComplete}
          />
        </div>
      ) : null}

      {tab === "actions" ? (
        <div className="app-details-panel">
        <InfoCard title="Deployment Actions">
          {!canDeployOps ? (
            <EmptyState message="Deployment actions require apps:deploy permission and access to this deployment in the namespace." />
          ) : (
            <>
              <div className="inventory-detail-actions">
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => {
                    setActionModal("restart");
                    setActionError("");
                  }}
                >
                  Restart deployment
                </button>
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => {
                    setScaleReplicas(String(summary.replicas ?? 1));
                    setActionModal("scale");
                    setActionError("");
                  }}
                >
                  Scale deployment
                </button>
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => {
                    setRollbackRevision("");
                    setActionModal("rollback");
                    setActionError("");
                  }}
                >
                  Rollback deployment
                </button>
              </div>

              <h4 className="eyebrow">Rollout history</h4>
              {historyLoading ? <p className="muted">Loading rollout history…</p> : null}
              {historyError ? <p className="banner-message error">{historyError}</p> : null}
              {(rolloutHistory?.revisions || rolloutHistory?.items || []).length ? (
                <DataTable
                  columns={[
                    { key: "revision", label: "Revision" },
                    { key: "changeCause", label: "Change cause" },
                  ]}
                  rows={(rolloutHistory.revisions || rolloutHistory.items || []).map((row) => ({
                    revision: row.revision ?? "—",
                    changeCause: row.changeCause || row.change_cause || "—",
                  }))}
                />
              ) : !historyLoading ? (
                <p className="muted">No rollout history returned.</p>
              ) : null}
            </>
          )}
        </InfoCard>
        </div>
      ) : null}

      <ConfirmActionModal
        open={Boolean(actionModal)}
        title={
          actionModal === "restart"
            ? "Restart deployment"
            : actionModal === "scale"
              ? "Scale deployment"
              : actionModal === "rollback"
                ? "Rollback deployment"
                : ""
        }
        message={
          actionModal === "restart"
            ? `Restart ${primaryWorkload} in ${summary.namespace}?`
            : actionModal === "scale"
              ? `Update desired replica count for ${primaryWorkload}.`
              : actionModal === "rollback"
                ? `Roll back ${primaryWorkload} to the previous revision, or specify a revision number.`
                : ""
        }
        confirmLabel={
          actionModal === "restart" ? "Restart" : actionModal === "scale" ? "Scale" : "Rollback"
        }
        danger={actionModal === "restart" || actionModal === "rollback"}
        busy={actionBusy}
        error={actionError}
        onClose={closeActionModal}
        onConfirm={runDeploymentAction}
      >
        {actionModal === "scale" ? (
          <label className="confirm-action-modal__field">
            Desired replicas
            <input type="number" min={0} max={50} value={scaleReplicas} onChange={(e) => setScaleReplicas(e.target.value)} />
          </label>
        ) : null}
        {actionModal === "rollback" ? (
          <label className="confirm-action-modal__field">
            Revision (optional)
            <input
              type="number"
              min={1}
              placeholder="Previous revision if empty"
              value={rollbackRevision}
              onChange={(e) => setRollbackRevision(e.target.value)}
            />
          </label>
        ) : null}
      </ConfirmActionModal>

      <ApplicationBuilderWizard
        open={deployWizardOpen}
        onClose={() => setDeployWizardOpen(false)}
        onSuccess={() => {
          setDeployWizardOpen(false);
          onRefreshDetail?.();
          onHelmActionComplete?.();
        }}
        clusterOptions={clusterOptions}
        defaultClusterId={summary.cluster}
        showTemplatePicker={false}
        initialState={{
          ...createEmptyWizardState(summary.cluster),
          basics: {
            ...createEmptyWizardState(summary.cluster).basics,
            appName: summary.applicationName || primaryWorkload,
            clusterId: summary.cluster,
            namespace: summary.namespace,
            ownerTeam: summary.ownerTeam || "",
            description: summary.description || catalog.description || "",
          },
          workloadType: summary.type || "Deployment",
        }}
      />

      <ResourceInspectModal
        open={inspectModal.open}
        title={inspectModal.title}
        loading={inspectModal.loading}
        error={inspectModal.error}
        mode={inspectModal.mode}
        content={inspectModal.content}
        rolloutRows={inspectModal.rolloutRows}
        onClose={closeInspectModal}
      />
    </div>
  );
}
