import { useCallback, useEffect, useRef, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import TopologyViewer from "../components/common/TopologyViewer.jsx";
import { getClusterTopology, getNamespaceTopology } from "../api/topologyApi.js";
import { EMPTY_MESSAGES } from "../utils/authz.js";

const EMPTY_GRAPH = { nodes: [], edges: [] };

/**
 * Automatic, per-cluster topology with a two-level drill-down:
 *   Level 1 — the cluster fanning out to its namespaces and worker nodes.
 *   Level 2 — click a namespace to open its pods (Ingress → Service → pod).
 * The cluster is chosen with the topbar's Active Cluster selector.
 */
export default function TopologyPage({
  clusterId,
  cluster,
  hasClusters = false,
  coreLoading = false,
  accessError = "",
}) {
  const [view, setView] = useState({ level: "cluster", namespace: "" });
  const [clusterTopo, setClusterTopo] = useState(null);
  const [nsTopo, setNsTopo] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // Monotonic request id so a slow response for a cluster/namespace the user has
  // already navigated away from can never overwrite the current view.
  const reqRef = useRef(0);

  const clusterName = cluster?.name || clusterId || "";

  const loadCluster = useCallback(() => {
    if (!clusterId) {
      setClusterTopo(null);
      return;
    }
    const seq = ++reqRef.current;
    setError("");
    setLoading(true);
    getClusterTopology(clusterId)
      .then((res) => {
        if (seq === reqRef.current) setClusterTopo(res?.topology || EMPTY_GRAPH);
      })
      .catch((err) => {
        if (seq === reqRef.current) setError(err.message || "Failed to load cluster topology.");
      })
      .finally(() => {
        if (seq === reqRef.current) setLoading(false);
      });
  }, [clusterId]);

  const openNamespace = useCallback(
    (namespace) => {
      if (!clusterId || !namespace) return;
      const seq = ++reqRef.current;
      setView({ level: "namespace", namespace });
      setNsTopo(null);
      setError("");
      setLoading(true);
      getNamespaceTopology(clusterId, namespace)
        .then((res) => {
          if (seq === reqRef.current) setNsTopo(res?.topology || EMPTY_GRAPH);
        })
        .catch((err) => {
          if (seq === reqRef.current)
            setError(err.message || "Failed to load namespace topology.");
        })
        .finally(() => {
          if (seq === reqRef.current) setLoading(false);
        });
    },
    [clusterId]
  );

  // Reset to the cluster view and (re)load Level 1 whenever the cluster changes.
  useEffect(() => {
    setView({ level: "cluster", namespace: "" });
    setNsTopo(null);
    setClusterTopo(null);
    loadCluster();
  }, [clusterId, loadCluster]);

  const backToCluster = () => {
    reqRef.current += 1; // cancel any in-flight namespace load
    setError("");
    setLoading(false);
    setView({ level: "cluster", namespace: "" });
  };

  const refresh = () => {
    if (view.level === "namespace") openNamespace(view.namespace);
    else loadCluster();
  };

  const handleNodeClick = (node) => {
    if (node?.kind === "namespace" && node.namespace) {
      openNamespace(node.namespace);
    }
  };

  const header = (
    <PageTitle
      title="Cluster Topology"
      subtitle="An automatic map of the selected cluster — drill into a namespace to see its pods."
    />
  );

  if (coreLoading) {
    return (
      <div className="ops-page topology-page">
        {header}
        <LoadingState label="Loading clusters..." />
      </div>
    );
  }

  if (!hasClusters || !clusterId) {
    return (
      <div className="ops-page topology-page">
        {header}
        <EmptyState message={EMPTY_MESSAGES.noClusters} />
      </div>
    );
  }

  const activeTopo = view.level === "namespace" ? nsTopo : clusterTopo;
  const hasGraph = Boolean(activeTopo && activeTopo.nodes && activeTopo.nodes.length);

  return (
    <div className="ops-page topology-page">
      {header}

      <div className="topo-breadcrumb">
        <div className="topo-crumbs">
          <button
            type="button"
            className={`topo-crumb${view.level === "cluster" ? " is-active" : ""}`}
            onClick={backToCluster}
            disabled={view.level === "cluster"}
          >
            {clusterName}
          </button>
          {view.level === "namespace" ? (
            <>
              <span className="topo-crumb-sep" aria-hidden="true">/</span>
              <span className="topo-crumb is-active">{view.namespace}</span>
            </>
          ) : null}
        </div>
        <div className="topo-crumb-actions">
          {view.level === "namespace" ? (
            <button type="button" className="btn-outline" onClick={backToCluster}>
              ← Back to cluster
            </button>
          ) : null}
          <button type="button" className="btn-outline" onClick={refresh} disabled={loading}>
            Refresh
          </button>
        </div>
      </div>

      <p className="topo-hint">
        {view.level === "cluster"
          ? "Click a namespace to open its pods · scroll to zoom · drag to pan"
          : "Scroll to zoom · drag to pan"}
      </p>

      {accessError ? <ErrorBanner message={accessError} /> : null}
      {error ? <ErrorBanner message={error} suppressAccessDenied={false} /> : null}

      {loading && !hasGraph ? (
        <LoadingState
          label={
            view.level === "namespace"
              ? `Building ${view.namespace} topology...`
              : "Building cluster topology..."
          }
        />
      ) : hasGraph ? (
        <TopologyViewer
          nodes={activeTopo.nodes}
          edges={activeTopo.edges}
          fillWidth
          zoomable
          onNodeClick={handleNodeClick}
          nodeClickable={(node) => node.kind === "namespace"}
        />
      ) : !loading && !error ? (
        <EmptyState
          message={
            view.level === "namespace"
              ? "This namespace has no pods, services, or ingresses to map."
              : "No topology to display for this cluster."
          }
        />
      ) : null}
    </div>
  );
}
