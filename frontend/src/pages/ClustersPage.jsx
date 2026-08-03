import { useState } from "react";
import { Link } from "react-router-dom";
import AccessScopeView from "../components/common/AccessScopeView.jsx";
import RequestDeploymentModal from "../components/clusters/RequestDeploymentModal.jsx";
import ConfigureRecipientsModal from "../components/clusters/ConfigureRecipientsModal.jsx";
import { useAuth } from "../context/AuthContext";
import { createDeploymentRequest } from "../api";
import { EMPTY_MESSAGES } from "../utils/authz.js";

// Cluster list statuses from the API are healthy / warning / unknown; keep the
// same tone mapping the old DataTable pill used so colours don't shift.
const STATUS_TONES = {
  healthy: "ok",
  warning: "warn",
  critical: "danger",
  error: "danger",
  unknown: "info",
};

const statusTone = (status) => STATUS_TONES[String(status).toLowerCase()] || "info";

const barFillClass = (value) => {
  if (value >= 95) return "sg-bar-fill sg-bar-fill--danger";
  if (value >= 85) return "sg-bar-fill sg-bar-fill--warn";
  return "sg-bar-fill";
};

function UsageBar({ label, value }) {
  const pct = Math.min(100, Math.max(0, value));
  return (
    <div className="sg-cs">
      <span>{label}</span>
      <div className="sg-bar-track">
        <div className={barFillClass(pct)} style={{ width: `${pct}%` }} />
      </div>
      <b>{Math.round(pct)}%</b>
    </div>
  );
}

function ActivityIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M22 12h-4l-3 8-6-16-3 8H2" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="m22 2-7 20-4-9-9-4Z" />
      <path d="M22 2 11 13" />
    </svg>
  );
}

function MailIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m3 7.5 9 6 9-6" />
    </svg>
  );
}

// "6 clusters · 94 nodes · Kubernetes v1.29.9 – v1.31.2" — only from real data;
// segments without backing values are dropped.
function buildSubtitle(clusters) {
  if (!clusters.length) {
    return "Track cluster lifecycle, availability, and capacity at a glance.";
  }
  const segments = [`${clusters.length} cluster${clusters.length === 1 ? "" : "s"}`];
  const totalNodes = clusters.reduce(
    (sum, cluster) => (typeof cluster.nodes === "number" ? sum + cluster.nodes : sum),
    0
  );
  if (totalNodes > 0) {
    segments.push(`${totalNodes} node${totalNodes === 1 ? "" : "s"}`);
  }
  const versions = [
    ...new Set(
      clusters
        .map((cluster) => cluster.k8sVersion || cluster.version)
        .filter((version) => version && version !== "unknown")
    ),
  ].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  if (versions.length === 1) {
    segments.push(`Kubernetes ${versions[0]}`);
  } else if (versions.length > 1) {
    segments.push(`Kubernetes ${versions[0]} – ${versions[versions.length - 1]}`);
  }
  return segments.join(" · ");
}

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

  const submitRequest = async (form) => {
    if (!activeCluster) return;
    const { message, windowStart, windowEnd, windowTimezone } =
      typeof form === "string" ? { message: form } : form;
    setSubmitting(true);
    setModalError("");
    try {
      const result = await createDeploymentRequest({
        cluster_id: activeCluster.id,
        cluster_name: activeCluster.name,
        message,
        windowStart,
        windowEnd,
        windowTimezone,
      });
      setActiveCluster(null);
      const emailResult = result?.emailResult;
      if (emailResult?.sent > 0) {
        setNotice(
          `Request sent to the management team (${emailResult.sent} recipient${
            emailResult.sent === 1 ? "" : "s"
          } notified). You can track it under My Requests.`
        );
      } else if (emailResult?.skipped) {
        setNotice(
          "Request submitted. It is pending review — email notification was skipped " +
            `(${emailResult.reason || "no recipients configured"}). You can track it under My Requests.`
        );
      } else {
        setNotice("Request submitted and is pending review. You can track it under My Requests.");
      }
    } catch (err) {
      setModalError(err.message || "Failed to send request.");
    } finally {
      setSubmitting(false);
    }
  };

  const clusters = (data?.clusters || []).filter((cluster) => cluster?.name || cluster?.id);
  const contentReady = !coreLoading && !accessError && hasClusters;

  const header = (
    <div className="sg-ph">
      <div>
        <h2>Clusters</h2>
        <p className="sg-ph-sub">{buildSubtitle(contentReady ? clusters : [])}</p>
      </div>
      {contentReady && canManageRecipients ? (
        <div className="sg-ph-actions">
          <button type="button" className="primary" onClick={() => setConfigureOpen(true)}>
            <MailIcon />
            Configure recipients
          </button>
        </div>
      ) : null}
    </div>
  );

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
      <div className="sg-card-grid">
        {clusters.map((cluster) => {
          const clusterRef = { id: cluster.id || cluster.name, name: cluster.name || cluster.id };
          const status = cluster.status || "unknown";
          const version = cluster.k8sVersion || cluster.version || "";
          const nodeCount = typeof cluster.nodes === "number" ? cluster.nodes : null;
          const headerSub = [
            version && version !== "unknown" ? version : null,
            nodeCount != null ? `${nodeCount} node${nodeCount === 1 ? "" : "s"}` : null,
          ]
            .filter(Boolean)
            .join(" · ");
          const cpu = typeof cluster.cpuUsage === "number" ? cluster.cpuUsage : null;
          const memory = typeof cluster.memoryUsage === "number" ? cluster.memoryUsage : null;
          const hasUsage = cpu != null || memory != null;

          return (
            <article key={clusterRef.id} className="sg-ccard">
              <header>
                <div>
                  {/*
                    The cluster overview screen existed with a full render path,
                    a data fetch, a tour and an RBAC gate, and nothing anywhere
                    linked to it (audit finding F3). This is that link.
                  */}
                  <Link className="sg-ccard-link" to={`/fleet/clusters/${clusterRef.id}`}>
                    <b>{clusterRef.name}</b>
                  </Link>
                  {headerSub ? <span className="sg-ccard-sub">{headerSub}</span> : null}
                </div>
                <span className={`status-pill ${statusTone(status)}`}>{status}</span>
              </header>
              {hasUsage ? (
                <div className="sg-ccard-body">
                  <div className="sg-cstats">
                    {cpu != null ? <UsageBar label="CPU" value={cpu} /> : null}
                    {memory != null ? <UsageBar label="Memory" value={memory} /> : null}
                  </div>
                </div>
              ) : (
                <p className="sg-cnote sg-cnote--muted">
                  <ActivityIcon />
                  Usage metrics unavailable for this cluster.
                </p>
              )}
              <footer>
                {cluster.provider ? <span className="sg-tag">{cluster.provider}</span> : null}
                {cluster.region && cluster.region !== "unknown" ? (
                  <span className="sg-tag">{cluster.region}</span>
                ) : null}
                {!canManageRecipients && canRequest ? (
                  <button
                    type="button"
                    className="sg-clusters-request"
                    onClick={() => openRequest(clusterRef)}
                  >
                    Request
                    <SendIcon />
                  </button>
                ) : null}
              </footer>
            </article>
          );
        })}
      </div>
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
        clusters={data?.clusters || []}
      />
    </AccessScopeView>
  );
}
