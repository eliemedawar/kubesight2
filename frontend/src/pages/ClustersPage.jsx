import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import DataTable from "../components/common/DataTable.jsx";
import { EMPTY_MESSAGES } from "../utils/authz.js";

export default function ClustersPage({ data, hasClusters, coreLoading = false, accessError = "" }) {
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
    .map((cluster) => ({
      name: cluster.name || cluster.id,
      status: cluster.status || "unknown",
      version: cluster.k8sVersion || cluster.version || "-",
      nodes: cluster.nodes ?? "-",
      cpuUsage: cluster.cpuUsage != null ? `${cluster.cpuUsage}%` : "-",
      memoryUsage: cluster.memoryUsage != null ? `${cluster.memoryUsage}%` : "-",
      action: "Connect",
    }));

  return (
    <AccessScopeView
      coreLoading={coreLoading}
      accessError={accessError}
      empty={!hasClusters}
      emptyMessage={EMPTY_MESSAGES.noClusters}
      loadingLabel="Loading clusters..."
      header={header}
    >
      <DataTable columns={columns} rows={rows} />
    </AccessScopeView>
  );
}
