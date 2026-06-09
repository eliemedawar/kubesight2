import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import DataTable from "../components/common/DataTable.jsx";
import { EMPTY_MESSAGES } from "../utils/authz.js";

export default function NamespacesPage({
  data,
  hasClusters,
  hasNamespaces,
  coreLoading = false,
  namespacesLoading = false,
  accessError = "",
}) {
  const header = (
    <PageTitle title="Namespaces" subtitle="Segment workloads and platform services by tenancy." />
  );

  const empty = !hasClusters
    ? { empty: true, message: EMPTY_MESSAGES.noClusters }
    : !hasNamespaces
      ? { empty: true, message: EMPTY_MESSAGES.noNamespaces }
      : { empty: false };

  const columns = [
    { key: "name", label: "Namespace" },
    { key: "pods", label: "Pods" },
    { key: "deployments", label: "Deployments" },
    { key: "services", label: "Services" },
    { key: "cpuUsage", label: "CPU Usage" },
    { key: "memoryUsage", label: "Memory Usage" },
    { key: "alerts", label: "Alerts" },
  ];

  return (
    <div className="ops-page">
      <AccessScopeView
        coreLoading={coreLoading}
        namespacesLoading={namespacesLoading}
        accessError={accessError}
        empty={empty.empty}
        emptyMessage={empty.message}
        header={header}
      >
        <DataTable
          columns={columns}
          rows={data.namespaces.map((ns) => ({
            ...ns,
            deployments: ns.deployments ?? ns.workloads?.deployments ?? "-",
            services: ns.services ?? ns.workloads?.services ?? "-",
            cpuUsage: ns.cpuUsage ?? (ns.cpuUsageCores != null ? `${ns.cpuUsageCores} cores` : "-"),
            memoryUsage: ns.memoryUsage ?? (ns.memoryUsageGiB != null ? `${ns.memoryUsageGiB} GiB` : "-"),
            alerts: ns.alerts ?? ns.alertCount ?? "-",
          }))}
        />
      </AccessScopeView>
    </div>
  );
}
