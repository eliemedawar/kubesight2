import { useState } from "react";
import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import DataTable from "../components/common/DataTable.jsx";
import RequestDeploymentModal from "../components/clusters/RequestDeploymentModal.jsx";
import ConfigureRecipientsModal from "../components/clusters/ConfigureRecipientsModal.jsx";
import { useAuth } from "../context/AuthContext";
import { createDeploymentRequest } from "../api";
import { EMPTY_MESSAGES } from "../utils/authz.js";

export default function ClustersPage({ data, hasClusters, coreLoading = false, accessError = "" }) {
  const { user, hasPermission } = useAuth();
  // Admins/managers configure who gets the request emails; everyone else requests.
  const canManageRecipients = hasPermission("deployment_requests:manage");
  const canRequest = hasPermission("deployment_requests:request");
  const requesterName = user?.fullName || user?.username || "";

  const [activeCluster, setActiveCluster] = useState(null);
  const [configureOpen, setConfigureOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [modalError, setModalError] = useState("");
  const [notice, setNotice] = useState("");

  const openRequest = (cluster) => {
    setModalError("");
    setActiveCluster(cluster);
  };

  const closeRequest = () => {
    if (submitting) return;
    setActiveCluster(null);
    setModalError("");
  };

  const submitRequest = async (message) => {
    if (!activeCluster) return;
    setSubmitting(true);
    setModalError("");
    try {
      const result = await createDeploymentRequest({
        cluster_id: activeCluster.id,
        cluster_name: activeCluster.name,
        message,
      });
      setActiveCluster(null);
      const emailResult = result?.emailResult;
      if (emailResult?.sent > 0) {
        setNotice(
          `Request sent to the management team (${emailResult.sent} recipient${
            emailResult.sent === 1 ? "" : "s"
          } notified).`
        );
      } else if (emailResult?.skipped) {
        setNotice(
          "Request submitted. It is pending review — email notification was skipped " +
            `(${emailResult.reason || "no recipients configured"}).`
        );
      } else {
        setNotice("Request submitted and is pending review.");
      }
    } catch (err) {
      setModalError(err.message || "Failed to send request.");
    } finally {
      setSubmitting(false);
    }
  };

  const header = (
    <PageTitle
      title="Clusters"
      subtitle="Track cluster lifecycle, availability, and capacity at a glance."
    />
  );

  const columns = [
    { key: "name", label: "Cluster" },
    { key: "status", label: "Status" },
    { key: "version", label: "Version" },
    { key: "nodes", label: "Nodes" },
    { key: "cpuUsage", label: "CPU Usage" },
    { key: "memoryUsage", label: "Memory Usage" },
    { key: "action", label: "Action" },
  ];
  const rows = (data?.clusters || [])
    .filter((cluster) => cluster?.name || cluster?.id)
    .map((cluster) => {
      const clusterRef = { id: cluster.id || cluster.name, name: cluster.name || cluster.id };
      return {
        name: clusterRef.name,
        status: cluster.status || "unknown",
        version: cluster.k8sVersion || cluster.version || "-",
        nodes: cluster.nodes ?? "-",
        cpuUsage: cluster.cpuUsage != null ? `${cluster.cpuUsage}%` : "-",
        memoryUsage: cluster.memoryUsage != null ? `${cluster.memoryUsage}%` : "-",
        action: canManageRecipients ? (
          <button
            type="button"
            className="btn-outline btn-compact"
            onClick={() => setConfigureOpen(true)}
          >
            Configure Recipients
          </button>
        ) : canRequest ? (
          <button
            type="button"
            className="btn-outline btn-compact"
            onClick={() => openRequest(clusterRef)}
          >
            Request
          </button>
        ) : (
          "-"
        ),
      };
    });

  return (
    <AccessScopeView
      coreLoading={coreLoading}
      accessError={accessError}
      empty={!hasClusters}
      emptyMessage={EMPTY_MESSAGES.noClusters}
      loadingLabel="Loading clusters..."
      header={header}
    >
      {notice ? (
        <p className="banner-message" role="status" style={{ marginBottom: "var(--space-3)" }}>
          {notice}
        </p>
      ) : null}
      <DataTable columns={columns} rows={rows} />
      <RequestDeploymentModal
        open={Boolean(activeCluster)}
        clusterName={activeCluster?.name || ""}
        requesterName={requesterName}
        busy={submitting}
        error={modalError}
        onClose={closeRequest}
        onSubmit={submitRequest}
      />
      <ConfigureRecipientsModal
        open={configureOpen}
        onClose={() => setConfigureOpen(false)}
      />
    </AccessScopeView>
  );
}
