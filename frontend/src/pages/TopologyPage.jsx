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
  const [warnings, setWarnings] = useState([]);
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
    setWarnings([]);
    setLoading(true);
    getClusterTopology(clusterId)
      .then((res) => {
        if (seq === reqRef.current) {
          setClusterTopo(res?.topology || EMPTY_GRAPH);
          setWarnings(res?.warnings || []);
        }
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
      setWarnings([]);
      setLoading(true);
      getNamespaceTopology(clusterId, namespace)
        .then((res) => {
          if (seq === reqRef.current) {
            setNsTopo(res?.topology || EMPTY_GRAPH);
            setWarnings(res?.warnings || []);
          }
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
    setWarnings([]);
    loadCluster();
  }, [clusterId, loadCluster]);

  const backToCluster = () => {
    reqRef.current += 1; // cancel any in-flight namespace load
    setError("");
    setWarnings([]);
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
  // Once the cluster graph has mounted, keep the same viewer instance alive
  // while drilling into a namespace. This preserves its fullscreen portal and
  // shows loading/empty feedback inside the canvas instead of unmounting it.
  const showViewer =
    hasGraph || (view.level === "namespace" && clusterTopo !== null);
  const activeNodes = activeTopo?.nodes || [];
  const countKind = (kind) => activeNodes.filter((node) => node.kind === kind).length;
  const issueCount = activeNodes.filter(
    (node) =>
      !["cluster", "group", "namespace-root"].includes(node.kind) &&
      ["degraded", "unhealthy"].includes(node.componentStatus)
  ).length;
  const summary =
    view.level === "namespace"
      ? [
          { label: "Pods", value: countKind("pod") },
          { label: "Services", value: countKind("service") },
          { label: "Ingresses", value: countKind("ingress") },
          { label: "Health issues", value: issueCount, issue: issueCount > 0 },
        ]
      : [
          { label: "Namespaces", value: countKind("namespace") },
          { label: "Nodes", value: countKind("node") },
          {
            label: "Healthy",
            value: activeNodes.filter(
              (node) =>
                ["namespace", "node"].includes(node.kind) &&
                node.componentStatus === "healthy"
            ).length,
          },
          { label: "Health issues", value: issueCount, issue: issueCount > 0 },
        ];

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
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {hasGraph ? (
        <>
          <div className="topo-summary" aria-label="Topology summary">
            {summary.map((item) => (
              <div
                key={item.label}
                className={`topo-stat${item.issue ? " topo-stat--issue" : ""}`}
              >
                <strong>{item.value}</strong>
                <span>{item.label}</span>
              </div>
            ))}
          </div>
          <div className="topo-guide">
            <p className="topo-hint">
              {view.level === "cluster"
                ? "Select a namespace to inspect its traffic path. Scroll to zoom and drag to pan."
                : "Traffic flows from the namespace through ingress and services to pods. Scroll to zoom and drag to pan."}
            </p>
            <div className="topo-legend" aria-label="Health legend">
              <span><i className="is-healthy" />Healthy</span>
              <span><i className="is-degraded" />Degraded</span>
              <span><i className="is-unhealthy" />Unhealthy</span>
              <span><i className="is-unknown" />Unknown</span>
            </div>
          </div>
        </>
      ) : null}

      {accessError ? <ErrorBanner message={accessError} /> : null}
      {error ? <ErrorBanner message={error} suppressAccessDenied={false} /> : null}
      {warnings.length ? (
        <p className="banner-message warning-banner topo-partial-warning">
          Showing a partial topology. {warnings.join(" ")}
        </p>
      ) : null}

      {showViewer ? (
        <TopologyViewer
          nodes={activeTopo?.nodes || []}
          edges={activeTopo?.edges || []}
          fillWidth
          zoomable
          loading={loading && !hasGraph}
          emptyMessage={
            error ||
            accessError ||
            "This namespace has no pods, services, or ingresses to map."
          }
          layoutDirection={view.level === "namespace" ? "packed" : "horizontal"}
          onNodeClick={handleNodeClick}
          nodeClickable={(node) => node.kind === "namespace"}
        />
      ) : loading ? (
        <LoadingState
          label={
            view.level === "namespace"
              ? `Building ${view.namespace} topology...`
              : "Building cluster topology..."
          }
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
