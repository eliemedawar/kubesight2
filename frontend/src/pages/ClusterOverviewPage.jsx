import AccessScopeView from "../components/common/AccessScopeView.jsx";
import { EMPTY_MESSAGES } from "../utils/authz.js";
import { parseApiTime } from "../lib/apiTime.js";
import {
  clusterFacts,
  countValue,
  cpuValue,
  memoryValue,
  statusTone,
  storageDetail,
  storageValue,
  usagePercent,
} from "../utils/clusterOverview.js";
import "../styles/signal/clusterOverview.css";

// One cluster's fact sheet: what it is, how big it is, what runs on it and the
// namespaces you can open. Triage (nodes, failing pods, alerts) stays on the
// Dashboard, which this page links to for the same cluster.

const meterTone = (pct) => (pct == null ? "" : pct >= 95 ? "danger" : pct >= 85 ? "warn" : "ok");

function Meter({ label, value, pct, note = "" }) {
  const usage = pct != null ? `${pct}% in use` : "Usage not reported";
  return (
    <div className="co-meter">
      <div className="co-meter-top">
        <span className="co-meter-label">{label}</span>
        <span className="co-meter-value">{value}</span>
      </div>
      <span className="db-bar" aria-hidden="true">
        {pct != null ? (
          <span className={`db-bar-fill db-bar-fill--${meterTone(pct)}`} style={{ width: `${Math.min(100, pct)}%` }} />
        ) : null}
      </span>
      <span className="co-meter-sub">{note ? `${usage} · ${note}` : usage}</span>
    </div>
  );
}

function formatTime(value) {
  const ms = parseApiTime(value);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function ClusterOverviewPage({
  cluster,
  overview,
  namespaces,
  hasClusters,
  coreLoading = false,
  namespacesLoading = false,
  accessError = "",
  canOpenPage = () => false,
}) {
  // The overview state is shared across clusters; never show another
  // cluster's figures while this one's are loading.
  const current = overview && (!overview.clusterId || overview.clusterId === cluster?.id) ? overview : null;
  const cpu = current?.resources?.cpu;
  const memory = current?.resources?.memory;
  const storage = current?.resources?.storage;
  const workloads = current?.workloads;
  const namespaceItems = (namespaces || []).filter((ns) => ns?.name);
  const clusterQuery = cluster ? `cluster=${encodeURIComponent(cluster.id)}` : "";
  const canOpenResources = canOpenPage("resources");
  const canOpenDashboard = canOpenPage("dashboard");

  const header = (
    <>
      <a className="co-back" href="#/clusters">
        <span aria-hidden="true">←</span> Back to Clusters
      </a>
      <div className="sg-ph">
        <div>
          <div className="co-title">
            <h2>{cluster ? cluster.name || cluster.id : "Cluster Overview"}</h2>
            {cluster ? (
              <span className={`status-pill ${statusTone(cluster.status || "unknown")}`}>
                {cluster.status || "unknown"}
              </span>
            ) : null}
          </div>
          <p className="sg-ph-sub">
            {cluster ? clusterFacts(cluster) || "Cluster overview" : "One cluster's capacity, workloads and namespaces."}
          </p>
        </div>
        {cluster && canOpenDashboard ? (
          <div className="sg-ph-actions">
            <a className="co-action" href={`#/dashboard?${clusterQuery}`}>
              Open dashboard
            </a>
          </div>
        ) : null}
      </div>
    </>
  );

  const cpuPct = usagePercent(cpu?.usedCores, cpu?.capacityCores);
  const memoryPct = usagePercent(memory?.usedGiB, memory?.capacityGiB);
  const storageUsed = storage?.usedGiB ?? storage?.claimedGiB;
  const storagePct = usagePercent(storageUsed, storage?.capacityGiB);

  const content = cluster ? (
    <div className="co-root">
      {!current ? (
        <p className="co-loading muted" role="status">
          Loading this cluster&apos;s figures…
        </p>
      ) : null}

      <div className="co-grid">
        <section className="co-card" aria-labelledby="co-capacity-title">
          <header className="co-card-h">
            <h3 id="co-capacity-title">Capacity</h3>
          </header>
          <div className="co-card-body co-meters">
            <Meter label="CPU" value={cpuValue(cpu)} pct={cpuPct} />
            <Meter label="Memory" value={memoryValue(memory)} pct={memoryPct} />
            <Meter
              label="Storage"
              value={storageValue(storage)}
              pct={storagePct}
              note={storageDetail(storage).toLowerCase()}
            />
          </div>
        </section>

        <section className="co-card" aria-labelledby="co-workloads-title">
          <header className="co-card-h">
            <h3 id="co-workloads-title">Workloads</h3>
          </header>
          <dl className="co-card-body co-counts">
            <div>
              <dt>Deployments</dt>
              <dd>{countValue(workloads?.deployments)}</dd>
            </div>
            <div>
              <dt>StatefulSets</dt>
              <dd>{countValue(workloads?.statefulsets)}</dd>
            </div>
            <div>
              <dt>DaemonSets</dt>
              <dd>{countValue(workloads?.daemonsets)}</dd>
            </div>
          </dl>
        </section>

        <section className="co-card" aria-labelledby="co-about-title">
          <header className="co-card-h">
            <h3 id="co-about-title">About</h3>
          </header>
          <dl className="co-card-body co-facts">
            <div>
              <dt>Provider</dt>
              <dd>{cluster.provider ? String(cluster.provider).toUpperCase() : "—"}</dd>
            </div>
            <div>
              <dt>Region</dt>
              <dd>{cluster.region && cluster.region !== "unknown" ? cluster.region : "—"}</dd>
            </div>
            <div>
              <dt>Kubernetes</dt>
              <dd className="co-mono">
                {(cluster.k8sVersion || cluster.version) && (cluster.k8sVersion || cluster.version) !== "unknown"
                  ? cluster.k8sVersion || cluster.version
                  : "—"}
              </dd>
            </div>
            <div>
              <dt>Nodes</dt>
              <dd>{countValue(typeof cluster.nodes === "number" ? cluster.nodes : null)}</dd>
            </div>
            <div>
              <dt>Last synced</dt>
              <dd>{formatTime(cluster.lastSync)}</dd>
            </div>
            <div>
              <dt>Figures as of</dt>
              <dd>{formatTime(current?.updatedAt)}</dd>
            </div>
          </dl>
        </section>
      </div>

      <section className="co-card" aria-labelledby="co-ns-title">
        <header className="co-card-h">
          <h3 id="co-ns-title">Namespaces in scope</h3>
          <span className="co-card-sub">{namespaceItems.length}</span>
        </header>
        {namespaceItems.length ? (
          <ul className="co-card-body co-ns">
            {namespaceItems.map((ns) => {
              const pods = typeof ns.pods === "number" ? `${ns.pods} pod${ns.pods === 1 ? "" : "s"}` : null;
              const body = (
                <>
                  <span className="co-ns-name">{ns.name}</span>
                  {pods ? <span className="co-ns-meta">{pods}</span> : null}
                </>
              );
              return (
                <li key={ns.name}>
                  {canOpenResources ? (
                    <a
                      className="co-ns-chip"
                      href={`#/resources/pods?${clusterQuery}&ns=${encodeURIComponent(ns.name)}`}
                      title={`Open ${ns.name} in Resources`}
                    >
                      {body}
                    </a>
                  ) : (
                    <span className="co-ns-chip">{body}</span>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="co-card-body co-empty">No namespaces you can see on this cluster.</p>
        )}
      </section>

      {canOpenDashboard ? (
        <p className="co-note">
          Nodes, failing pods and alerts for this cluster are triaged on the{" "}
          <a href={`#/dashboard?${clusterQuery}`}>Dashboard</a>.
        </p>
      ) : null}
    </div>
  ) : null;

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
        {content}
      </AccessScopeView>
    </div>
  );
}
