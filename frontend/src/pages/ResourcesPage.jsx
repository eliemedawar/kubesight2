import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import DataTable from "../components/common/DataTable.jsx";
import ResourceRowActions from "../components/resources/ResourceRowActions.jsx";
import {
  getDeploymentRolloutHistory,
  getResourceDescribe,
  getResourceYaml,
} from "../api/clustersApi.js";
import { RESOURCE_TAB_DEFINITIONS, listKeyForTab } from "../lib/resourceTypes.js";
import { EMPTY_MESSAGES, formatAccessError } from "../utils/authz.js";
import { usePermission } from "../hooks/usePermission.js";

const ResourceInspectModal = lazy(() => import("../components/resources/ResourceInspectModal.jsx"));
const EditDeploymentModal = lazy(() => import("../components/resources/EditDeploymentModal.jsx"));

const CLOSED_EDIT_MODAL = {
  open: false,
  clusterId: "",
  namespace: "",
  deploymentName: "",
};

const POD_FILTER_WORKLOAD_KINDS = new Set([
  "deployment",
  "replicaset",
  "statefulset",
  "daemonset",
  "job",
]);

function podsForWorkload(pods, workloadName) {
  if (!workloadName) {
    return pods;
  }
  const prefix = `${workloadName}-`;
  return pods.filter((pod) => pod.name === workloadName || pod.name.startsWith(prefix));
}

function buildPodActionsCell(pod, onResourceAction) {
  let actions = Array.isArray(pod.actions) ? [...pod.actions] : [];
  if (pod.canViewLogs === false) {
    actions = actions.filter((action) => action !== "logs" && action !== "view-logs");
  }
  return (
    <ResourceRowActions
      actions={actions}
      fallback={actions.length ? null : "details"}
      onAction={(actionId) => onResourceAction(actionId, pod, "pod")}
    />
  );
}

function buildWorkloadActionsCell(resource, resourceKind, onResourceAction, { fallback = "pods" } = {}) {
  if (Array.isArray(resource.actions)) {
    return (
      <ResourceRowActions
        actions={resource.actions}
        onAction={(actionId) => onResourceAction(actionId, resource, resourceKind)}
      />
    );
  }
  if (typeof resource.actions === "string" && resource.actions.trim()) {
    return (
      <ResourceRowActions
        actions={resource.actions}
        onAction={(actionId) => onResourceAction(actionId, resource, resourceKind)}
      />
    );
  }
  return (
    <ResourceRowActions
      fallback={fallback}
      onAction={(actionId) => onResourceAction(actionId, resource, resourceKind)}
    />
  );
}

function formatUsageLimit(usage, limit) {
  const u = usage && usage !== "-" ? usage : "—";
  const l = limit && limit !== "-" ? limit : "—";
  return `${u} / ${l}`;
}

function getEmptyTabMessage({
  title,
  namespace,
  workloadPodFilter,
  workloadFilterLabel,
  isAdmin,
  rawCount = 0,
  filteredCount = 0,
}) {
  if (workloadPodFilter?.name) {
    return `No pods found for ${workloadFilterLabel} ${workloadPodFilter.name}.`;
  }
  const label = (title || "resources").toLowerCase();
  const ns = namespace || "selected";
  if (!isAdmin && rawCount > filteredCount) {
    return `No ${label} match your access rules in the ${ns} namespace.`;
  }
  return `No ${label} in the ${ns} namespace.`;
}

function buildServiceActionsCell(svc, onResourceAction) {
  if (Array.isArray(svc.actions)) {
    return <ResourceRowActions actions={svc.actions} onAction={(actionId) => onResourceAction(actionId, svc, "service")} />;
  }
  if (typeof svc.actions === "string") {
    return <ResourceRowActions actions={svc.actions} onAction={(actionId) => onResourceAction(actionId, svc, "service")} />;
  }
  return <ResourceRowActions onAction={(actionId) => onResourceAction(actionId, svc, "service")} />;
}

const CLOSED_MODAL = {
  open: false,
  title: "",
  mode: "text",
  loading: false,
  error: "",
  content: "",
  rolloutRows: [],
};

export default function ResourcesPage({
  data,
  clusterId,
  namespace,
  hasClusters,
  hasNamespaces,
  coreLoading = false,
  namespacesLoading = false,
  activeTab: activeTabProp = "",
  onActiveTabChange,
  onRefreshTab,
  tabLoading = false,
  tabRefreshing = false,
  isTabLoaded = () => false,
  tabErrors = {},
  accessError = "",
  visibleTabs = RESOURCE_TAB_DEFINITIONS.map((def) => def.tabKey),
  isAdmin = false,
  rawResources = null,
  onNavigateToLogs,
}) {
  const tabKeys = useMemo(
    () =>
      visibleTabs.filter((tab) => RESOURCE_TAB_DEFINITIONS.some((def) => def.tabKey === tab)),
    [visibleTabs]
  );
  const [internalActiveTab, setInternalActiveTab] = useState(tabKeys[0] || "pods");
  const activeTab = activeTabProp || internalActiveTab;
  const setActiveTab = onActiveTabChange || setInternalActiveTab;
  const { hasPermission } = usePermission();
  const canDeploy = hasPermission("apps:deploy");
  const [workloadPodFilter, setWorkloadPodFilter] = useState({ name: "", kind: "" });
  const [inspectModal, setInspectModal] = useState(CLOSED_MODAL);
  const [editModal, setEditModal] = useState(CLOSED_EDIT_MODAL);

  useEffect(() => {
    if (!tabKeys.includes(activeTab)) {
      setActiveTab(tabKeys[0] || "pods");
    }
  }, [tabKeys, activeTab]);

  const closeInspectModal = useCallback(() => {
    setInspectModal(CLOSED_MODAL);
  }, []);

  const closeEditModal = useCallback(() => {
    setEditModal(CLOSED_EDIT_MODAL);
  }, []);

  const openTextInspect = useCallback(async ({ title, mode, fetchContent }) => {
    setInspectModal({
      ...CLOSED_MODAL,
      open: true,
      title,
      mode,
      loading: true,
    });
    try {
      const payload = await fetchContent();
      const content =
        mode === "yaml"
          ? payload.yaml || payload.output || ""
          : payload.output || payload.yaml || "";
      setInspectModal((prev) => ({
        ...prev,
        loading: false,
        content,
      }));
    } catch (err) {
      setInspectModal((prev) => ({
        ...prev,
        loading: false,
        error: formatAccessError(err.message) || err.message || "Request failed",
      }));
    }
  }, []);

  const handleResourceAction = useCallback(
    (actionId, resource, resourceKind) => {
      const resolvedCluster = resource.cluster || resource.clusterId || clusterId;
      const resolvedNamespace = resource.namespace || namespace;
      const resourceName = resource.name;

      if (actionId === "logs") {
        onNavigateToLogs?.({
          clusterId: resolvedCluster,
          namespace: resolvedNamespace,
          pod: resourceName,
        });
        return;
      }

      if (actionId === "pods" && POD_FILTER_WORKLOAD_KINDS.has(resourceKind)) {
        setWorkloadPodFilter({ name: resourceName, kind: resourceKind });
        if (tabKeys.includes("pods")) {
          setActiveTab("pods");
        }
        return;
      }

      if (actionId === "describe" || actionId === "details") {
        openTextInspect({
          title: `Describe ${resourceKind} / ${resourceName}`,
          mode: "describe",
          fetchContent: () =>
            getResourceDescribe({
              clusterId: resolvedCluster,
              namespace: resolvedNamespace,
              kind: resourceKind,
              name: resourceName,
            }),
        });
        return;
      }

      if (actionId === "yaml") {
        openTextInspect({
          title: `YAML — ${resourceName}`,
          mode: "yaml",
          fetchContent: () =>
            getResourceYaml({
              clusterId: resolvedCluster,
              namespace: resolvedNamespace,
              kind: resourceKind,
              name: resourceName,
            }),
        });
        return;
      }

      if (actionId === "edit" && resourceKind === "deployment") {
        setEditModal({
          open: true,
          clusterId: resolvedCluster,
          namespace: resolvedNamespace,
          deploymentName: resourceName,
        });
        return;
      }

      if (actionId === "rollout" && resourceKind === "deployment") {
        setInspectModal({
          ...CLOSED_MODAL,
          open: true,
          title: `Rollout history — ${resourceName}`,
          mode: "rollout",
          loading: true,
        });
        getDeploymentRolloutHistory({
          clusterId: resolvedCluster,
          namespace: resolvedNamespace,
          deploymentName: resourceName,
        })
          .then((payload) => {
            const revisions = payload.revisions || payload.items || [];
            setInspectModal((prev) => ({
              ...prev,
              loading: false,
              rolloutRows: revisions.map((row, index) => ({
                id: `${row.revision ?? index}`,
                revision: row.revision ?? "—",
                changeCause: row.changeCause || "—",
              })),
            }));
          })
          .catch((err) => {
            setInspectModal((prev) => ({
              ...prev,
              loading: false,
              error: formatAccessError(err.message) || err.message || "Request failed",
            }));
          });
      }
    },
    [clusterId, namespace, onNavigateToLogs, openTextInspect, tabKeys]
  );

  const header = (
    <PageTitle title="Resources" subtitle="Inspect workload objects across cluster namespaces." />
  );

  const scopeEmpty = !hasClusters
    ? { empty: true, message: EMPTY_MESSAGES.noClusters }
    : !hasNamespaces
      ? { empty: true, message: EMPTY_MESSAGES.noNamespaces }
      : { empty: false };

  const podSource = data.resources.pods || [];
  const visiblePods = workloadPodFilter.name
    ? podsForWorkload(podSource, workloadPodFilter.name)
    : podSource;

  const tableConfig = {
    pods: {
      title: "Pods",
      columns: [
        { key: "name", label: "Pod" },
        { key: "status", label: "Status" },
        { key: "ready", label: "Ready" },
        { key: "restarts", label: "Restarts" },
        { key: "cpu", label: "CPU (use/limit)" },
        { key: "memory", label: "Memory (use/limit)" },
        { key: "age", label: "Age" },
        { key: "image", label: "Image" },
        { key: "node", label: "Node" },
        { key: "actions", label: "Actions" },
      ],
      rows: visiblePods.map((pod) => {
        const cpuUsage = pod.cpuUsage || (pod.cpuUsageMillicores != null ? `${pod.cpuUsageMillicores}m` : "-");
        const memoryUsage = pod.memoryUsage || (pod.memoryUsageMiB != null ? `${pod.memoryUsageMiB}Mi` : "-");
        const cpuLimit = pod.cpuLimit || (pod.cpuLimitMillicores != null ? `${pod.cpuLimitMillicores}m` : "-");
        const memoryLimit = pod.memoryLimit || (pod.memoryLimitMiB != null ? `${pod.memoryLimitMiB}Mi` : "-");
        return {
          ...pod,
          cluster: pod.cluster || clusterId,
          namespace: pod.namespace || namespace,
          ready: pod.ready || "-",
          cpu: formatUsageLimit(cpuUsage, cpuLimit),
          memory: formatUsageLimit(memoryUsage, memoryLimit),
          age: pod.age || "-",
          image: pod.image || "-",
          node: pod.node || "-",
          actions: buildPodActionsCell(pod, handleResourceAction),
        };
      }),
    },
    deployments: {
      title: "Deployments",
      columns: [
        { key: "name", label: "Deployment" },
        { key: "desired", label: "Desired" },
        { key: "ready", label: "Ready" },
        { key: "available", label: "Available" },
        { key: "image", label: "Image" },
        { key: "updateStatus", label: "Status" },
        { key: "upToDate", label: "Updated" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.deployments || []).map((dep) => ({
        ...dep,
        cluster: dep.cluster || clusterId,
        namespace: dep.namespace || namespace,
        desired: dep.desired ?? dep.replicas?.desired ?? "-",
        ready: dep.ready ?? dep.replicas?.ready ?? "-",
        available: dep.available ?? dep.replicas?.ready ?? "-",
        updateStatus: dep.updateStatus || dep.status || "-",
        upToDate: dep.upToDate ?? "-",
        age: dep.age || "-",
        actions: buildWorkloadActionsCell(
          {
            ...dep,
            actions: canDeploy
              ? [...(dep.actions || ["pods", "rollout", "yaml"]), "edit"]
              : dep.actions,
          },
          "deployment",
          handleResourceAction
        ),
      })),
    },
    replicaSets: {
      title: "ReplicaSets",
      columns: [
        { key: "name", label: "ReplicaSet" },
        { key: "owner", label: "Owner" },
        { key: "desired", label: "Desired" },
        { key: "ready", label: "Ready" },
        { key: "image", label: "Image" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.replicasets || []).map((rs) => ({
        ...rs,
        cluster: rs.cluster || clusterId,
        namespace: rs.namespace || namespace,
        owner: rs.owner || "-",
        desired: rs.desired ?? "-",
        ready: rs.ready ?? "-",
        age: rs.age || "-",
        actions: buildWorkloadActionsCell(rs, "replicaset", handleResourceAction),
      })),
    },
    statefulSets: {
      title: "StatefulSets",
      columns: [
        { key: "name", label: "StatefulSet" },
        { key: "desired", label: "Desired" },
        { key: "ready", label: "Ready" },
        { key: "available", label: "Available" },
        { key: "image", label: "Image" },
        { key: "updateStatus", label: "Status" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.statefulsets || []).map((sts) => ({
        ...sts,
        cluster: sts.cluster || clusterId,
        namespace: sts.namespace || namespace,
        desired: sts.desired ?? "-",
        ready: sts.ready ?? "-",
        available: sts.available ?? "-",
        updateStatus: sts.updateStatus || sts.status || "-",
        age: sts.age || "-",
        actions: buildWorkloadActionsCell(sts, "statefulset", handleResourceAction),
      })),
    },
    daemonSets: {
      title: "DaemonSets",
      columns: [
        { key: "name", label: "DaemonSet" },
        { key: "desired", label: "Desired" },
        { key: "ready", label: "Ready" },
        { key: "available", label: "Available" },
        { key: "image", label: "Image" },
        { key: "updateStatus", label: "Status" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.daemonsets || []).map((ds) => ({
        ...ds,
        cluster: ds.cluster || clusterId,
        namespace: ds.namespace || namespace,
        desired: ds.desired ?? "-",
        ready: ds.ready ?? "-",
        available: ds.available ?? "-",
        updateStatus: ds.updateStatus || ds.status || "-",
        age: ds.age || "-",
        actions: buildWorkloadActionsCell(ds, "daemonset", handleResourceAction),
      })),
    },
    jobs: {
      title: "Jobs",
      columns: [
        { key: "name", label: "Job" },
        { key: "status", label: "Status" },
        { key: "completions", label: "Completions" },
        { key: "image", label: "Image" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.jobs || []).map((job) => ({
        ...job,
        cluster: job.cluster || clusterId,
        namespace: job.namespace || namespace,
        status: job.status || "-",
        completions: job.completions || "-",
        age: job.age || "-",
        actions: buildWorkloadActionsCell(job, "job", handleResourceAction),
      })),
    },
    cronJobs: {
      title: "CronJobs",
      columns: [
        { key: "name", label: "CronJob" },
        { key: "schedule", label: "Schedule" },
        { key: "suspend", label: "Suspended" },
        { key: "active", label: "Active" },
        { key: "lastSchedule", label: "Last schedule" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.cronjobs || []).map((cj) => ({
        ...cj,
        cluster: cj.cluster || clusterId,
        namespace: cj.namespace || namespace,
        schedule: cj.schedule || "-",
        suspend: cj.suspend ? "Yes" : "No",
        active: cj.active ?? "-",
        lastSchedule: cj.lastSchedule || "-",
        age: cj.age || "-",
        actions: buildWorkloadActionsCell(cj, "cronjob", handleResourceAction, { fallback: "details" }),
      })),
    },
    services: {
      title: "Services",
      columns: [
        { key: "name", label: "Service" },
        { key: "type", label: "Type" },
        { key: "clusterIP", label: "Cluster IP" },
        { key: "externalIP", label: "External IP" },
        { key: "ports", label: "Ports" },
        { key: "targetPods", label: "Targets" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.services || []).map((svc) => ({
        ...svc,
        cluster: svc.cluster || clusterId,
        namespace: svc.namespace || namespace,
        clusterIP: svc.clusterIP || "-",
        externalIP: svc.externalIP || "-",
        ports: Array.isArray(svc.ports) ? svc.ports.join(", ") : (svc.ports || "-"),
        targetPods: svc.targetPods || "-",
        age: svc.age || "-",
        actions: buildServiceActionsCell(svc, handleResourceAction),
      })),
    },
  };

  const visibleTableConfig = Object.fromEntries(
    Object.entries(tableConfig).filter(([key]) => tabKeys.includes(key))
  );
  const active = visibleTableConfig[activeTab] || visibleTableConfig[tabKeys[0]];

  const workloadFilterLabel = workloadPodFilter.kind
    ? workloadPodFilter.kind.replace(/([A-Z])/g, " $1").trim()
    : "workload";

  const activeListKey = activeTab ? listKeyForTab(activeTab) : "";
  const rawResourceList = rawResources?.[activeListKey] || [];
  const filteredResourceList = data.resources?.[activeListKey] || [];
  const activeTabError = tabErrors[activeListKey] || "";
  const activeTabHasBeenLoaded = activeListKey ? isTabLoaded(activeListKey) : false;

  return (
    <div className="ops-page resources-page">
      <AccessScopeView
        coreLoading={coreLoading}
        namespacesLoading={namespacesLoading}
        accessError={accessError}
        empty={scopeEmpty.empty}
        emptyMessage={scopeEmpty.message}
        header={header}
      >
      {!tabKeys.length ? (
        <EmptyState
          message={EMPTY_MESSAGES.noResources}
          hint="Your role does not include workload or service visibility."
        />
      ) : (
        <>
          <div className="resources-tab-toolbar">
            <nav className="tab-bar tab-bar--scroll" aria-label="resource-tabs">
              {Object.entries(visibleTableConfig).map(([key, config]) => (
                <button
                  key={key}
                  type="button"
                  className={key === activeTab ? "active" : ""}
                  onClick={() => {
                    setActiveTab(key);
                    if (key !== "pods") {
                      setWorkloadPodFilter({ name: "", kind: "" });
                    }
                  }}
                >
                  {config.title}
                </button>
              ))}
            </nav>
            {onRefreshTab ? (
              <button
                type="button"
                className="btn-outline btn-sm resources-tab-refresh"
                onClick={onRefreshTab}
                disabled={tabLoading || tabRefreshing}
                aria-busy={tabRefreshing}
              >
                {tabRefreshing ? "Refreshing…" : "Refresh"}
              </button>
            ) : null}
          </div>
          {activeTabError ? (
            <p className="muted empty-state-inline resource-tab-error">{activeTabError}</p>
          ) : null}
          {tabLoading && !activeTabHasBeenLoaded ? (
            <LoadingState label={`Loading ${active?.title || "resources"}...`} />
          ) : null}
          {workloadPodFilter.name && activeTab === "pods" ? (
            <p className="muted resource-pod-filter-hint">
              Showing pods for {workloadFilterLabel}{" "}
              <strong>{workloadPodFilter.name}</strong>.{" "}
              <button
                type="button"
                className="btn-text"
                onClick={() => setWorkloadPodFilter({ name: "", kind: "" })}
              >
                Clear filter
              </button>
            </p>
          ) : null}
          {!tabLoading || activeTabHasBeenLoaded ? (
            !active || active.rows.length === 0 ? (
              <p className="muted empty-state-inline">
                {getEmptyTabMessage({
                  title: active?.title,
                  namespace,
                  workloadPodFilter,
                  workloadFilterLabel,
                  isAdmin,
                  rawCount: rawResourceList.length,
                  filteredCount: filteredResourceList.length,
                })}
              </p>
            ) : (
              <DataTable columns={active.columns} rows={active.rows} tableClassName="resources-table" />
            )
          ) : null}
          {activeTab === "services" &&
          tabKeys.includes("services") &&
          (data.resources.services || []).some((s) => s.canViewPorts === false || !(s.ports || []).length) ? (
            <p className="muted empty-state-inline">
              Some service ports are hidden — you need services:ports:view and an access rule for each port.
            </p>
          ) : null}
        </>
      )}
      {inspectModal.open ? (
        <Suspense fallback={null}>
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
        </Suspense>
      ) : null}
      {editModal.open ? (
        <Suspense fallback={null}>
          <EditDeploymentModal
            open={editModal.open}
            clusterId={editModal.clusterId}
            namespace={editModal.namespace}
            deploymentName={editModal.deploymentName}
            onClose={closeEditModal}
            onSuccess={() => {
              onRefreshTab?.();
              closeEditModal();
            }}
          />
        </Suspense>
      ) : null}
      </AccessScopeView>
    </div>
  );
}
