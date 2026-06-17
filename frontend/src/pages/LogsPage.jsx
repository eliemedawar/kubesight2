import { useCallback, useEffect, useRef, useState } from "react";
import { listNamespacePodsForLogs, listPodContainers } from "../api/clustersApi.js";
import LogFilters from "../components/logs/LogFilters.jsx";
import LogViewer from "../components/logs/LogViewer.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import useLogsViewer from "../hooks/useLogsViewer.js";
import { isNamespaceScopeLoading, SCOPE_LOADING_HINT } from "../utils/accessViewState.js";
import { EMPTY_MESSAGES, formatAccessError, isAccessDeniedError } from "../utils/authz.js";

const DEFAULT_FILTERS = {
  cluster: "",
  namespace: "",
  pod: "",
  container: "",
  timeRange: "15m",
  customFrom: "",
  customTo: "",
  searchText: "",
  levelFilter: "all",
  previous: false,
};

export default function LogsPage({
  clusters,
  namespaces,
  selectedClusterId,
  selectedNamespace,
  preferredPod = "",
  onPreferredPodApplied,
  onClusterChange,
  onNamespaceChange,
  hasClusters,
  hasNamespaces,
  coreLoading = false,
  namespacesLoading = false,
  accessError = "",
}) {
  const [filters, setFilters] = useState({ ...DEFAULT_FILTERS });
  const [pods, setPods] = useState([]);
  const [containers, setContainers] = useState([]);
  const [podsLoading, setPodsLoading] = useState(false);
  const [containersLoading, setContainersLoading] = useState(false);
  const [podsError, setPodsError] = useState("");
  const [showTimestamps, setShowTimestamps] = useState(true);
  const jumpRef = useRef(null);

  const updateFilters = useCallback((patch) => {
    setFilters((prev) => ({ ...prev, ...patch }));
  }, []);

  useEffect(() => {
    setFilters((prev) => ({
      ...prev,
      cluster: selectedClusterId || "",
      namespace: selectedNamespace || "",
      pod: "",
      container: "",
    }));
  }, [selectedClusterId, selectedNamespace]);

  useEffect(() => {
    if (!filters.cluster || !filters.namespace) {
      setPods([]);
      setContainers([]);
      setPodsError("");
      setFilters((prev) => ({ ...prev, pod: "", container: "" }));
      return undefined;
    }

    let cancelled = false;
    setPodsLoading(true);
    setPodsError("");

    listNamespacePodsForLogs(filters.cluster, filters.namespace)
      .then((payload) => {
        if (cancelled) {
          return;
        }
        const items = (payload.items || []).filter((pod) => pod.canViewLogs !== false);
        setPods(items);

        setFilters((prev) => {
          const preferredName = preferredPod || "";
          const resolvedPod = preferredName
            ? items.find((pod) => pod.name === preferredName)?.name
            : items.find((pod) => pod.name === prev.pod)?.name || items[0]?.name || "";
          return {
            ...prev,
            pod: resolvedPod,
            container: resolvedPod === prev.pod ? prev.container : "",
          };
        });

        if (preferredPod) {
          onPreferredPodApplied?.();
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setPods([]);
          setPodsError(error.message);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setPodsLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [filters.cluster, filters.namespace, preferredPod, onPreferredPodApplied]);

  useEffect(() => {
    if (!filters.cluster || !filters.namespace || !filters.pod) {
      setContainers([]);
      setFilters((prev) => ({ ...prev, container: "" }));
      return undefined;
    }

    let cancelled = false;
    setContainersLoading(true);

    listPodContainers(filters.cluster, filters.namespace, filters.pod)
      .then((payload) => {
        if (cancelled) {
          return;
        }
        const items = payload.items || [];
        setContainers(items);
        setFilters((prev) => {
          const stillValid = items.some((item) => item.name === prev.container);
          return {
            ...prev,
            container: stillValid ? prev.container : items[0]?.name || "",
          };
        });
      })
      .catch(() => {
        if (!cancelled) {
          setContainers([]);
          updateFilters({ container: "" });
        }
      })
      .finally(() => {
        if (!cancelled) {
          setContainersLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [filters.cluster, filters.namespace, filters.pod]);

  const {
    logLines,
    loading: logsLoading,
    error: logsError,
    liveEnabled,
    setLiveEnabled,
    refreshLogs,
    clearLogs,
    hasTarget,
  } = useLogsViewer(filters, { enabled: hasClusters && hasNamespaces });

  const handlePodChange = (podName) => {
    updateFilters({ pod: podName, container: "" });
  };

  const scopeLoading = isNamespaceScopeLoading({
    coreLoading,
    namespacesLoading,
    resourcesLoading: false,
  });

  let scopeBody = null;
  if (scopeLoading) {
    const scopeLabel = coreLoading ? "Loading clusters…" : "Loading namespaces…";
    scopeBody = <LoadingState label={scopeLabel} hint={SCOPE_LOADING_HINT} />;
  } else if (isAccessDeniedError(accessError)) {
    scopeBody = <AccessDeniedPage message={accessError} />;
  } else if (accessError) {
    scopeBody = (
      <p className="banner-message error">
        {formatAccessError(accessError, { suppressAccessDenied: false })}
      </p>
    );
  } else if (!hasClusters) {
    scopeBody = <EmptyState message={EMPTY_MESSAGES.noClusters} />;
  } else if (!hasNamespaces) {
    scopeBody = <EmptyState message={EMPTY_MESSAGES.noNamespaces} />;
  }

  const canShowFilters = !scopeLoading && !accessError && hasClusters && hasNamespaces;

  return (
    <div className="ops-page">
      <PageTitle
        title="Logs"
        subtitle="View Kubernetes pod and container logs with live streaming and filters."
      />
      {scopeBody}
      {canShowFilters ? (
        <>
          <LogFilters
            filters={filters}
            onChange={updateFilters}
            clusters={clusters}
            namespaces={namespaces}
            pods={pods}
            containers={containers}
            podsLoading={podsLoading}
            containersLoading={containersLoading}
            liveEnabled={liveEnabled}
            onLiveToggle={() => setLiveEnabled((enabled) => !enabled)}
            showTimestamps={showTimestamps}
            onTimestampsToggle={() => setShowTimestamps((value) => !value)}
            onPreviousToggle={() => updateFilters({ previous: !filters.previous })}
            onClusterChange={onClusterChange}
            onNamespaceChange={onNamespaceChange}
            onPodChange={handlePodChange}
          />
          {podsError ? <p className="banner-message error">{formatAccessError(podsError)}</p> : null}
          {!podsLoading && !pods.length && !podsError ? (
            <EmptyState message={EMPTY_MESSAGES.noLogPods} />
          ) : (
            <LogViewer
              lines={logLines}
              live={liveEnabled}
              streaming={liveEnabled && hasTarget}
              loading={logsLoading}
              error={formatAccessError(logsError)}
              searchText={filters.searchText}
              levelFilter={filters.levelFilter}
              showTimestamps={showTimestamps}
              onRefresh={refreshLogs}
              onClear={clearLogs}
              onJumpToLatestRef={jumpRef}
              emptyMessage={
                hasTarget
                  ? "No logs found for the selected container."
                  : "Select a pod and container to view logs."
              }
            />
          )}
        </>
      ) : null}
    </div>
  );
}
