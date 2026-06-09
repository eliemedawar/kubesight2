import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import StatCard from "../components/common/StatCard.jsx";
import InfoCard from "../components/common/InfoCard.jsx";
import { EMPTY_MESSAGES } from "../utils/authz.js";

export default function ClusterOverviewPage({
  cluster,
  overview,
  namespaces,
  hasClusters,
  coreLoading = false,
  namespacesLoading = false,
  accessError = "",
}) {
  const header = (
    <PageTitle
      title="Cluster Overview"
      subtitle={
        cluster
          ? `Deep dive for ${cluster.name} (${String(cluster.provider || "").toUpperCase()})`
          : "Deep dive for your assigned clusters."
      }
    />
  );

  const cpu = overview?.resources?.cpu;
  const memory = overview?.resources?.memory;

  return (
    <div className="ops-page">
      <AccessScopeView
        coreLoading={coreLoading}
        namespacesLoading={namespacesLoading}
        accessError={accessError}
        empty={!hasClusters || !cluster}
        emptyMessage={EMPTY_MESSAGES.noClusters}
        header={header}
      >
        <section className="stat-grid">
          <StatCard title="Status" value={cluster.status} detail={`Region ${cluster.region}`} />
          <StatCard
            title="Kubernetes"
            value={cluster.k8sVersion || cluster.version || "-"}
            detail="Current control plane version"
          />
          <StatCard
            title="CPU Usage"
            value={
              cpu
                ? `${Number(cpu.usedCores).toFixed(2)}/${Number(cpu.capacityCores).toFixed(2)} cores`
                : "-"
            }
            detail="Live usage from kubectl top"
          />
          <StatCard
            title="Memory Usage"
            value={
              memory
                ? `${Number(memory.usedGiB).toFixed(2)}/${Number(memory.capacityGiB).toFixed(2)} GiB`
                : "-"
            }
            detail="Live usage from kubectl top"
          />
        </section>
        <InfoCard title="Namespaces in Scope">
          <div className="pill-row">
            {namespaces.map((ns) => (
              <span key={ns.name} className="pill">
                {ns.name}
              </span>
            ))}
            {!namespaces.length ? (
              <span className="pill">No namespaces available</span>
            ) : null}
          </div>
        </InfoCard>
      </AccessScopeView>
    </div>
  );
}
