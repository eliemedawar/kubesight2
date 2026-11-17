import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import DataTable from "../components/common/DataTable.jsx";
import ResourceRowActions from "../components/resources/ResourceRowActions.jsx";
import ResourceKindRail from "../components/resources/ResourceKindRail.jsx";
import ResourceAttentionStrip from "../components/resources/ResourceAttentionStrip.jsx";
import { buildExtraKindTables } from "../components/resources/extraKindTables.jsx";
import {
  getClusterPodIssues,
  getDeploymentRolloutHistory,
  getResourceDescribe,
  getResourceYaml,
  restartResource,
} from "../api/clustersApi.js";
import {
  RESOURCE_GROUPS,
  RESOURCE_TAB_DEFINITIONS,
  listKeyForTab,
  listKeyHasTone,
  rowTone,
  tabDefinition,
  tabKeyForListKey,
} from "../lib/resourceTypes.js";
import { useResourceSummary } from "../hooks/useResourceSummary.js";
import { EMPTY_MESSAGES, formatAccessError } from "../utils/authz.js";
import { usePermission } from "../hooks/usePermission.js";
import { isPendingApproval, pendingApprovalMessage } from "../utils/pendingApproval.js";
import { secretValuesHiddenNote } from "../utils/sensitiveResponses.js";
import { confirmDialog } from "../lib/dialogs.js";

const ResourceInspectModal = lazy(() => import("../components/resources/ResourceInspectModal.jsx"));
const EditResourceModal = lazy(() => import("../components/resources/EditResourceModal.jsx"));
const ExecPodModal = lazy(() => import("../components/resources/ExecPodModal.jsx"));

const CLOSED_EXEC_MODAL = {
  open: false,
  clusterId: "",
  namespace: "",
  pod: "",
  containers: [],
};

// Resource kinds that the Edit & apply modal can load/edit.
const EDITABLE_KINDS = new Set(["deployment", "configmap", "secret", "ingress"]);

const CLOSED_EDIT_MODAL = {
  open: false,
  clusterId: "",
  namespace: "",
  kind: "",
  resourceName: "",
};

const POD_FILTER_WORKLOAD_KINDS = new Set([
  "deployment",
  "replicaset",
  "statefulset",
  "daemonset",
  "job",
]);

/**
 * The quick filters above a kind's table. The first one is where the kind
 * opens. Rows "needing attention" follow rowTone (lib/resourceTypes.js), the
 * same rule the server counts the rail badges with.
 */
function segmentsFor(tabKey, listKey, namespace) {
  if (tabKey === "events") {
    return [
      { id: "warnings", label: "Warnings" },
      { id: "all", label: "All" },
    ];
  }
  if (tabKey === "pvs") {
    return [
      { id: "here", label: `Claimed from ${namespace || "this namespace"}` },
      { id: "all", label: "Whole cluster" },
    ];
  }
  if (listKeyHasTone(listKey)) {
    return [
      { id: "all", label: "All" },
      { id: "issues", label: "Needs attention" },
    ];
  }
  return [];
}

function rowInSegment(segmentId, listKey, row, namespace) {
  if (segmentId === "issues" || segmentId === "warnings") {
    return rowTone(listKey, row, namespace) !== "";
  }
  if (segmentId === "here") {
    return row.claimNamespace === namespace;
  }
  return true;
}

function segmentEmptyMessage(segmentId, title, namespace) {
  const ns = namespace || "this namespace";
  if (segmentId === "warnings") {
    return { text: `No warning events in ${ns} right now.`, action: "Show all events" };
  }
  if (segmentId === "here") {
    return { text: `No volumes are claimed from ${ns}.`, action: "Show the whole cluster" };
  }
  return {
    text: `Nothing in ${(title || "this list").toLowerCase()} needs attention in ${ns}.`,
    action: "Show all",
  };
}

function podsForWorkload(pods, workloadName) {
  if (!workloadName) {
    return pods;
  }
  const prefix = `${workloadName}-`;
  return pods.filter((pod) => pod.name === workloadName || pod.name.startsWith(prefix));
}

function buildPodActionsCell(pod, onResourceAction, canRestart = false) {
  let actions = Array.isArray(pod.actions) ? [...pod.actions] : [];
  if (pod.canViewLogs === false) {
    actions = actions.filter((action) => action !== "logs" && action !== "view-logs");
  }
  if (canRestart && !actions.includes("restart")) {
    actions.push("restart");
  }
  if (canRestart && !actions.includes("exec")) {
    actions.push("exec");
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
  tabKey,
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
  if (tabKey === "storageClasses") {
    return "This cluster has no storage classes.";
  }
  if (tabKey === "pvs") {
    return isAdmin
      ? "This cluster has no volumes."
      : "No volumes are claimed from the namespaces you can see.";
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
  note: "",
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
  live = false,
  liveIntervalMs = 5000,
  onLiveChange,
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
  // One source only: the route, handed down by App. The old internal fallback
  // could disagree with the prop, which is exactly the desync routing removes.
  const activeTab = activeTabProp || tabKeys[0] || "pods";
  const setActiveTab = onActiveTabChange || (() => {});
  const { hasPermission } = usePermission();
  const canDeploy = hasPermission("apps:deploy");
  const [workloadPodFilter, setWorkloadPodFilter] = useState({ name: "", kind: "" });
  const [search, setSearch] = useState("");
  // The quick filter chosen per kind ({ pods: "issues", events: "all", … });
  // a kind not in here opens on its first segment.
  const [segmentByTab, setSegmentByTab] = useState({});
  const [allNsMode, setAllNsMode] = useState(false);
  const [allNsData, setAllNsData] = useState({ pods: [], loading: false, error: "" });
  const [inspectModal, setInspectModal] = useState(CLOSED_MODAL);
  const [editModal, setEditModal] = useState(CLOSED_EDIT_MODAL);
  const [execModal, setExecModal] = useState(CLOSED_EXEC_MODAL);

  useEffect(() => {
    if (!tabKeys.includes(activeTab)) {
      // A correction, not a destination — do not leave it in history.
      setActiveTab(tabKeys[0] || "pods", { replace: true });
    }
  }, [tabKeys, activeTab, setActiveTab]);

  const { summary, refresh: refreshSummary } = useResourceSummary({
    clusterId,
    namespace,
    enabled: Boolean(hasClusters && hasNamespaces && clusterId && namespace && tabKeys.length),
    live,
  });

  // A kind picked from the rail opens clean: its own search, no pod filter.
  const selectKind = useCallback(
    (tabKey) => {
      setActiveTab(tabKey);
      setSearch("");
      if (tabKey !== "pods") {
        setWorkloadPodFilter({ name: "", kind: "" });
      }
    },
    [setActiveTab]
  );

  // A link in a cell (an event's object, a claim's pod) opens that kind
  // searched for the object, with nothing filtering it out.
  const jumpTo = useCallback(
    (tabKey, query) => {
      setActiveTab(tabKey);
      setSearch(query || "");
      setWorkloadPodFilter({ name: "", kind: "" });
      setSegmentByTab((prev) => ({ ...prev, [tabKey]: "all" }));
    },
    [setActiveTab]
  );

  const openAttentionItem = useCallback(
    (item) => {
      setActiveTab(item.tabKey);
      setSearch("");
      setWorkloadPodFilter({ name: "", kind: "" });
      setSegmentByTab((prev) => ({ ...prev, [item.tabKey]: "issues" }));
    },
    [setActiveTab]
  );

  const canJump = useCallback((tabKey) => tabKeys.includes(tabKey), [tabKeys]);

  const closeInspectModal = useCallback(() => {
    setInspectModal(CLOSED_MODAL);
  }, []);

  const closeEditModal = useCallback(() => {
    setEditModal(CLOSED_EDIT_MODAL);
  }, []);

  const closeExecModal = useCallback(() => {
    setExecModal(CLOSED_EXEC_MODAL);
  }, []);

  const loadAllNsIssues = useCallback(async ({ background = false } = {}) => {
    if (!clusterId) {
      return;
    }
    if (!background) {
      setAllNsData((prev) => ({ ...prev, loading: true, error: "" }));
    }
    try {
      const payload = await getClusterPodIssues(clusterId);
      setAllNsData({ pods: payload.pods || [], loading: false, error: "" });
    } catch (err) {
      // A failed live tick keeps the last scan on screen.
      if (background) {
        return;
      }
      setAllNsData({
        pods: [],
        loading: false,
        error: formatAccessError(err.message) || err.message || "Failed to scan namespaces",
      });
    }
  }, [clusterId]);

  // Cross-namespace issues are pods-only; leaving the Pods tab exits the mode.
  useEffect(() => {
    if (allNsMode && activeTab !== "pods") {
      setAllNsMode(false);
    }
  }, [allNsMode, activeTab]);

  useEffect(() => {
    if (allNsMode) {
      loadAllNsIssues();
    }
  }, [allNsMode, loadAllNsIssues]);

  // The per-namespace tabs go live in the resource cache; the cross-namespace
  // scan is local to this page, so it polls here (a little slower — it walks every namespace).
  useEffect(() => {
    if (!allNsMode || !live) {
      return undefined;
    }
    let busy = false;
    const timer = window.setInterval(async () => {
      if (busy || document.hidden) {
        return;
      }
      busy = true;
      try {
        await loadAllNsIssues({ background: true });
      } finally {
        busy = false;
      }
    }, liveIntervalMs * 2);
    return () => window.clearInterval(timer);
  }, [allNsMode, live, liveIntervalMs, loadAllNsIssues]);

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
        note: mode === "yaml" ? secretValuesHiddenNote(payload) : "",
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
    async (actionId, resource, resourceKind) => {
      const resolvedCluster = resource.cluster || resource.clusterId || clusterId;
      const resolvedNamespace = resource.namespace || namespace;
      const resourceName = resource.name;

      // For a workload the Logs page opens its first pod (name prefix match).
      if (actionId === "logs" || actionId === "view-logs") {
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

      if (actionId === "edit" && EDITABLE_KINDS.has(resourceKind)) {
        setEditModal({
          open: true,
          clusterId: resolvedCluster,
          namespace: resolvedNamespace,
          kind: resourceKind,
          resourceName,
        });
        return;
      }

      if (actionId === "exec" && resourceKind === "pod") {
        setExecModal({
          open: true,
          clusterId: resolvedCluster,
          namespace: resolvedNamespace,
          pod: resourceName,
          containers: Array.isArray(resource.containers) ? resource.containers : [],
        });
        return;
      }

      if (actionId === "restart") {
        const verb =
          resourceKind === "pod" ? "Restart (delete & recreate)" : "Rollout restart";
        const confirmed = await confirmDialog({
          title: resourceKind === "pod" ? "Restart pod" : "Rollout restart",
          message: `${verb} ${resourceKind} "${resourceName}"?`,
          confirmLabel: "Restart",
          tone: "danger",
        });
        if (!confirmed) {
          return;
        }
        setInspectModal({
          ...CLOSED_MODAL,
          open: true,
          title: `Restart — ${resourceName}`,
          mode: "describe",
          loading: true,
        });
        restartResource({
          clusterId: resolvedCluster,
          namespace: resolvedNamespace,
          kind: resourceKind,
          name: resourceName,
        })
          .then((payload) => {
            setInspectModal((prev) => ({
              ...prev,
              loading: false,
              content: isPendingApproval(payload)
                ? pendingApprovalMessage(payload)
                : payload.output || `${resourceKind}/${resourceName} restarted.`,
            }));
            onRefreshTab?.();
          })
          .catch((err) => {
            setInspectModal((prev) => ({
              ...prev,
              loading: false,
              error: formatAccessError(err.message) || err.message || "Restart failed",
            }));
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
    [clusterId, namespace, onNavigateToLogs, onRefreshTab, openTextInspect, tabKeys]
  );

  const isAllNsPodsHeader = allNsMode && activeTab === "pods";
  const header = (
    <PageTitle
      title="Resources"
      subtitle="Everything in a namespace, grouped by what it does."
      actions={
        tabKeys.length && hasClusters && hasNamespaces ? (
          <>
            {onLiveChange ? (
              <button
                type="button"
                className={`btn-outline btn-sm resources-live-toggle${live ? " active" : ""}`}
                onClick={() => onLiveChange(!live)}
                aria-pressed={live}
                title={
                  live
                    ? `Live: statuses update every ${Math.round(liveIntervalMs / 1000)}s. Click to pause.`
                    : "Paused: click to update statuses automatically"
                }
              >
                <span className="resources-live-dot" aria-hidden="true" />
                {live ? "Live" : "Paused"}
              </button>
            ) : null}
            {onRefreshTab ? (
              <button
                type="button"
                className="btn-outline btn-sm resources-tab-refresh"
                onClick={() => {
                  if (isAllNsPodsHeader) {
                    loadAllNsIssues();
                  } else {
                    onRefreshTab();
                  }
                  refreshSummary();
                }}
                disabled={isAllNsPodsHeader ? allNsData.loading : tabLoading || tabRefreshing}
                aria-busy={isAllNsPodsHeader ? allNsData.loading : tabRefreshing}
              >
                {(isAllNsPodsHeader ? allNsData.loading : tabRefreshing) ? "Refreshing…" : "Refresh"}
              </button>
            ) : null}
          </>
        ) : null
      }
    />
  );

  const scopeEmpty = !hasClusters
    ? { empty: true, message: EMPTY_MESSAGES.noClusters }
    : !hasNamespaces
      ? { empty: true, message: EMPTY_MESSAGES.noNamespaces }
      : { empty: false };

  const isAllNsPods = allNsMode && activeTab === "pods";
  const podSource = data.resources.pods || [];
  const namespaceScopedPods = workloadPodFilter.name
    ? podsForWorkload(podSource, workloadPodFilter.name)
    : podSource;
  const visiblePods = isAllNsPods ? allNsData.pods : namespaceScopedPods;

  const tableConfig = {
    pods: {
      title: "Pods",
      columns: [
        { key: "name", label: "Pod" },
        ...(isAllNsPods ? [{ key: "namespace", label: "Namespace" }] : []),
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
          actions: buildPodActionsCell(pod, handleResourceAction, canDeploy),
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
              ? [...(dep.actions || ["pods", "rollout", "yaml"]), "restart", "edit"]
              : dep.actions,
          },
          "deployment",
          handleResourceAction
        ),
      })),
    },
    configMaps: {
      title: "ConfigMaps",
      columns: [
        { key: "name", label: "ConfigMap" },
        { key: "keys", label: "Keys" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.configmaps || []).map((cm) => ({
        ...cm,
        cluster: cm.cluster || clusterId,
        namespace: cm.namespace || namespace,
        keys: cm.keys ?? "-",
        age: cm.age || "-",
        actions: buildWorkloadActionsCell(
          {
            ...cm,
            actions: canDeploy
              ? [...(cm.actions || ["describe", "yaml"]), "edit"]
              : cm.actions || ["describe", "yaml"],
          },
          "configmap",
          handleResourceAction,
          { fallback: "yaml" }
        ),
      })),
    },
    secrets: {
      title: "Secrets",
      columns: [
        { key: "name", label: "Secret" },
        { key: "type", label: "Type" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.secrets || []).map((sec) => ({
        ...sec,
        cluster: sec.cluster || clusterId,
        namespace: sec.namespace || namespace,
        type: sec.type || "Opaque",
        age: sec.age || "-",
        actions: buildWorkloadActionsCell(
          {
            ...sec,
            actions: canDeploy
              ? [...(sec.actions || ["describe", "yaml"]), "edit"]
              : sec.actions || ["describe", "yaml"],
          },
          "secret",
          handleResourceAction,
          { fallback: "yaml" }
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
        actions: buildWorkloadActionsCell(
          {
            ...sts,
            actions: canDeploy ? [...(sts.actions || ["pods"]), "restart"] : sts.actions,
          },
          "statefulset",
          handleResourceAction
        ),
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
        actions: buildWorkloadActionsCell(
          {
            ...ds,
            actions: canDeploy ? [...(ds.actions || ["pods"]), "restart"] : ds.actions,
          },
          "daemonset",
          handleResourceAction
        ),
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
    ingresses: {
      title: "Ingresses",
      columns: [
        { key: "name", label: "Ingress" },
        { key: "host", label: "Host" },
        { key: "path", label: "Path" },
        { key: "backendService", label: "Backend service" },
        { key: "tls", label: "TLS" },
        { key: "age", label: "Age" },
        { key: "actions", label: "Actions" },
      ],
      rows: (data.resources.ingress || []).map((ing) => ({
        ...ing,
        cluster: ing.cluster || clusterId,
        namespace: ing.namespace || namespace,
        host: ing.host || "-",
        path: ing.path || "-",
        backendService: ing.backendService || "-",
        tls: ing.tlsEnabled ? "Yes" : "No",
        age: ing.age || "-",
        actions: buildWorkloadActionsCell(
          {
            ...ing,
            actions: canDeploy
              ? [...(ing.actions || ["describe", "yaml"]), "edit"]
              : ing.actions || ["describe", "yaml"],
          },
          "ingress",
          handleResourceAction,
          { fallback: "yaml" }
        ),
      })),
    },
  };

  Object.assign(
    tableConfig,
    buildExtraKindTables({
      resources: data.resources,
      clusterId,
      namespace,
      onResourceAction: handleResourceAction,
      onJump: jumpTo,
      canJump,
    })
  );

  const visibleTableConfig = Object.fromEntries(
    Object.entries(tableConfig).filter(([key]) => tabKeys.includes(key))
  );
  const resolvedTab = visibleTableConfig[activeTab] ? activeTab : tabKeys[0];
  const active = visibleTableConfig[resolvedTab];
  const activeDef = tabDefinition(resolvedTab);
  const activeRowsListKey = listKeyForTab(resolvedTab);

  const segments = segmentsFor(resolvedTab, activeRowsListKey, namespace);
  const activeSegment =
    segments.find((seg) => seg.id === segmentByTab[resolvedTab])?.id || segments[0]?.id || "all";

  const searchTerm = search.trim().toLowerCase();
  const segmentCounts = useMemo(() => {
    const rows = active?.rows || [];
    return Object.fromEntries(
      segments.map((seg) => [
        seg.id,
        rows.filter((row) => rowInSegment(seg.id, activeRowsListKey, row, namespace)).length,
      ])
    );
    // `segments` is rebuilt each render from these same inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, activeRowsListKey, namespace, resolvedTab]);

  const filteredRows = useMemo(() => {
    if (!active) {
      return [];
    }
    let rows = active.rows;
    if (activeSegment !== "all") {
      rows = rows.filter((row) => rowInSegment(activeSegment, activeRowsListKey, row, namespace));
    }
    if (!searchTerm) {
      return rows;
    }
    return rows.filter((row) => {
      // Rows whose cells are links or pills say what to search in `searchText`.
      if (typeof row.searchText === "string") {
        return row.searchText.toLowerCase().includes(searchTerm);
      }
      return active.columns.some((col) => {
        if (col.key === "actions") {
          return false;
        }
        const value = row[col.key];
        if (value == null || typeof value === "object") {
          return false;
        }
        return String(value).toLowerCase().includes(searchTerm);
      });
    });
  }, [active, searchTerm, activeSegment, activeRowsListKey, namespace]);

  // Side rail: per kind, its count and how many rows need attention. The open
  // kind counts from the rows on screen (live every few seconds); the others
  // from the namespace summary (every 30 s).
  const railGroups = RESOURCE_GROUPS.map((group) => ({
    key: group.key,
    label: group.label,
    items: group.tabs
      .filter((tabKey) => tabKeys.includes(tabKey))
      .map((tabKey) => {
        const def = tabDefinition(tabKey);
        const listKey = def.listKey;
        let count = null;
        let issues = 0;
        let tone = "";
        if (tabKey === resolvedTab && isTabLoaded(listKey) && !(allNsMode && tabKey === "pods")) {
          const rows = data.resources?.[listKey] || [];
          count = tabKey === "pvs" ? rows.filter((row) => row.claimNamespace === namespace).length : rows.length;
          for (const row of rows) {
            const rowLevel = rowTone(listKey, row, namespace);
            if (rowLevel) {
              issues += 1;
              tone = tone === "crit" || rowLevel === "crit" ? "crit" : "warn";
            }
          }
        } else if (summary.kinds?.[listKey]) {
          ({ count, issues, tone } = summary.kinds[listKey]);
        }
        return { tabKey, title: def.title, clusterScoped: Boolean(def.clusterScoped), count, issues, tone };
      }),
  })).filter((group) => group.items.length);

  const attentionItems = (summary.attention || [])
    .map((item) => {
      const tabKey = tabKeyForListKey(item.listKey);
      return {
        ...item,
        key: `${item.listKey}/${item.name}`,
        tabKey,
        kindTitle: tabDefinition(tabKey)?.title || item.listKey,
      };
    })
    .filter((item) => item.tabKey && tabKeys.includes(item.tabKey));

  const workloadFilterLabel = workloadPodFilter.kind
    ? workloadPodFilter.kind.replace(/([A-Z])/g, " $1").trim()
    : "workload";

  const activeListKey = resolvedTab ? listKeyForTab(resolvedTab) : "";
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
          <ResourceAttentionStrip
            items={attentionItems}
            total={Math.max(summary.attentionTotal || 0, attentionItems.length)}
            onOpen={openAttentionItem}
          />
          <div className="resources-layout">
            <ResourceKindRail groups={railGroups} activeTab={resolvedTab} onSelect={selectKind} />
            <section className="resources-main" aria-labelledby="resources-kind-title">
              <header className="resources-kind-head">
                <div className="resources-kind-head__top">
                  <h3 id="resources-kind-title" className="resources-kind-head__title">
                    {activeDef?.title || active?.title}
                  </h3>
                  {activeDef?.apiKind ? (
                    <span className="resources-kind-head__api">{activeDef.apiKind}</span>
                  ) : null}
                  {isAllNsPods ? (
                    <span className="resources-scope-tag resources-scope-tag--cluster">
                      All namespaces · pods with problems
                    </span>
                  ) : activeDef?.clusterScoped ? (
                    <span className="resources-scope-tag resources-scope-tag--cluster">
                      Cluster-wide · the namespace picker doesn’t narrow it
                    </span>
                  ) : (
                    <span className="resources-scope-tag">Namespace · {namespace}</span>
                  )}
                </div>
                {activeDef?.describe ? (
                  <p className="resources-kind-head__desc">{activeDef.describe(namespace || "this namespace")}</p>
                ) : null}
                <div className="resources-kind-tools">
                  <input
                    type="search"
                    className="resource-search-input"
                    placeholder={`Search ${(activeDef?.title || "resources").toLowerCase()}…`}
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    aria-label={`Search ${(activeDef?.title || "resources").toLowerCase()}`}
                  />
                  {segments.length ? (
                    <div className="resources-segments" role="group" aria-label="Show">
                      {segments.map((seg) => (
                        <button
                          key={seg.id}
                          type="button"
                          className={seg.id === activeSegment ? "is-on" : ""}
                          aria-pressed={seg.id === activeSegment}
                          onClick={() => setSegmentByTab((prev) => ({ ...prev, [resolvedTab]: seg.id }))}
                        >
                          {seg.label}
                          {segmentCounts[seg.id] != null ? (
                            <span className="resources-segments__count">{segmentCounts[seg.id]}</span>
                          ) : null}
                        </button>
                      ))}
                    </div>
                  ) : null}
                  {resolvedTab === "pods" ? (
                    <button
                      type="button"
                      className={`btn-outline btn-sm resources-allns-filter${allNsMode ? " active" : ""}`}
                      onClick={() => setAllNsMode((prev) => !prev)}
                      aria-pressed={allNsMode}
                      title="Scan every namespace in this cluster for pods with problems (CrashLoopBackOff, ImagePullBackOff, Pending, Error…)"
                    >
                      All namespaces
                    </button>
                  ) : null}
                </div>
              </header>

              <div className="resources-main__body">
                {isAllNsPods ? (
                  <>
                    <p className="muted resource-pod-filter-hint">
                      Showing pods with problems across <strong>all namespaces</strong> in this cluster.{" "}
                      <button type="button" className="btn-text" onClick={() => setAllNsMode(false)}>
                        Back to {namespace || "namespace"}
                      </button>
                    </p>
                    {allNsData.loading ? (
                      <LoadingState label="Scanning all namespaces for problems..." />
                    ) : allNsData.error ? (
                      <p className="muted empty-state-inline resource-tab-error">{allNsData.error}</p>
                    ) : filteredRows.length === 0 ? (
                      searchTerm ? (
                        <p className="muted empty-state-inline">
                          No pods match “{search.trim()}”.{" "}
                          <button type="button" className="btn-text" onClick={() => setSearch("")}>
                            Clear search
                          </button>
                        </p>
                      ) : (
                        <p className="muted empty-state-inline">
                          No pods with problems in any namespace of this cluster.
                        </p>
                      )
                    ) : (
                      <DataTable
                        columns={active.columns}
                        rows={filteredRows}
                        tableClassName="resources-table resources-table--allns"
                        pageSize={Math.max(filteredRows.length, 1)}
                      />
                    )}
                  </>
                ) : (
                  <>
                    {activeTabError ? (
                      <p className="muted empty-state-inline resource-tab-error">{activeTabError}</p>
                    ) : null}
                    {tabLoading && !activeTabHasBeenLoaded ? (
                      <LoadingState label={`Loading ${(activeDef?.title || "resources").toLowerCase()}...`} />
                    ) : null}
                    {workloadPodFilter.name && resolvedTab === "pods" ? (
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
                        // An error above already says why there are no rows.
                        activeTabError ? null : (
                        <p className="muted empty-state-inline">
                          {getEmptyTabMessage({
                            tabKey: resolvedTab,
                            title: activeDef?.title || active?.title,
                            namespace,
                            workloadPodFilter,
                            workloadFilterLabel,
                            isAdmin,
                            rawCount: rawResourceList.length,
                            filteredCount: filteredResourceList.length,
                          })}
                        </p>
                        )
                      ) : filteredRows.length === 0 ? (
                        searchTerm ? (
                          <p className="muted empty-state-inline">
                            No {(activeDef?.title || "resources").toLowerCase()} match “{search.trim()}”.{" "}
                            <button type="button" className="btn-text" onClick={() => setSearch("")}>
                              Clear search
                            </button>
                          </p>
                        ) : (
                          (() => {
                            const empty = segmentEmptyMessage(activeSegment, activeDef?.title, namespace);
                            return (
                              <p className="muted empty-state-inline">
                                {empty.text}{" "}
                                <button
                                  type="button"
                                  className="btn-text"
                                  onClick={() =>
                                    setSegmentByTab((prev) => ({ ...prev, [resolvedTab]: "all" }))
                                  }
                                >
                                  {empty.action}
                                </button>
                              </p>
                            );
                          })()
                        )
                      ) : (
                        <DataTable
                          columns={active.columns}
                          rows={filteredRows}
                          tableClassName={`resources-table resources-table--${resolvedTab}`}
                          // While searching or filtered, show every match on one
                          // page so a result is never hidden on a later page.
                          pageSize={
                            searchTerm || activeSegment !== "all"
                              ? Math.max(filteredRows.length, 1)
                              : undefined
                          }
                        />
                      )
                    ) : null}
                  </>
                )}
                {resolvedTab === "services" &&
                (data.resources.services || []).some((s) => s.canViewPorts === false || !(s.ports || []).length) ? (
                  <p className="muted empty-state-inline">
                    Some service ports are hidden — you need services:ports:view and an access rule for each port.
                  </p>
                ) : null}
              </div>
            </section>
          </div>
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
            note={inspectModal.note}
            onClose={closeInspectModal}
          />
        </Suspense>
      ) : null}
      {editModal.open ? (
        <Suspense fallback={null}>
          <EditResourceModal
            open={editModal.open}
            clusterId={editModal.clusterId}
            namespace={editModal.namespace}
            kind={editModal.kind}
            resourceName={editModal.resourceName}
            onClose={closeEditModal}
            onSuccess={() => {
              onRefreshTab?.();
              closeEditModal();
            }}
          />
        </Suspense>
      ) : null}
      {execModal.open ? (
        <Suspense fallback={null}>
          <ExecPodModal
            open={execModal.open}
            clusterId={execModal.clusterId}
            namespace={execModal.namespace}
            pod={execModal.pod}
            containers={execModal.containers}
            onClose={closeExecModal}
          />
        </Suspense>
      ) : null}
      </AccessScopeView>
    </div>
  );
}
