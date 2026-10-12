import { useState } from "react";
import InfoCard from "../common/InfoCard.jsx";
import ConfirmActionModal from "./ConfirmActionModal.jsx";
import { rollbackHelmRelease, uninstallHelmRelease } from "../../api/helmApi.js";
import { isPendingApproval, pendingApprovalMessage } from "../../utils/pendingApproval.js";

// Helm reports times as ISO strings or "2026-10-11 09:12:03.1 +0000 UTC";
// show them in the viewer's locale, and anything unparseable as-is.
function formatDeployedAt(value) {
  if (!value) return value;
  const text = String(value).replace(/ \+0000 UTC$/, "Z").replace(" ", "T");
  const time = Date.parse(text);
  return Number.isFinite(time) ? new Date(time).toLocaleString() : value;
}

function DetailRow({ label, value }) {
  return (
    <div className="upgrade-detail-row">
      <span className="upgrade-detail-label">{label}</span>
      <span className="upgrade-detail-value">{value ?? "—"}</span>
    </div>
  );
}

export default function HelmReleasePanel({
  helm,
  summary,
  canViewHelm = false,
  canUpgradeHelm = false,
  canRollbackHelm = false,
  canUninstallHelm = false,
  onUpgrade,
  onViewManifest,
  onActionComplete,
}) {
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [rollbackBusy, setRollbackBusy] = useState(false);
  const [rollbackError, setRollbackError] = useState("");
  // Set when a rollback/uninstall was sent for approval instead of run.
  const [queuedMessage, setQueuedMessage] = useState("");
  const [uninstallOpen, setUninstallOpen] = useState(false);
  const [uninstallBusy, setUninstallBusy] = useState(false);
  const [uninstallError, setUninstallError] = useState("");

  if (!helm && summary?.source !== "Helm") {
    return null;
  }

  const data = helm || {};
  const valuesKeys = Object.keys(data.valuesSummary || {});

  const handleRollback = async () => {
    setRollbackBusy(true);
    setRollbackError("");
    try {
      const result = await rollbackHelmRelease({
        clusterId: summary.cluster,
        namespace: summary.namespace,
        releaseName: data.releaseName || summary.applicationName,
      });
      setRollbackOpen(false);
      if (isPendingApproval(result)) {
        setQueuedMessage(pendingApprovalMessage(result));
        return;
      }
      onActionComplete?.();
    } catch (err) {
      setRollbackError(err.message || "Rollback failed");
    } finally {
      setRollbackBusy(false);
    }
  };

  const handleUninstall = async () => {
    setUninstallBusy(true);
    setUninstallError("");
    try {
      const result = await uninstallHelmRelease({
        clusterId: summary.cluster,
        namespace: summary.namespace,
        releaseName: data.releaseName || summary.applicationName,
      });
      setUninstallOpen(false);
      if (isPendingApproval(result)) {
        setQueuedMessage(pendingApprovalMessage(result));
        return;
      }
      onActionComplete?.();
    } catch (err) {
      setUninstallError(err.message || "Uninstall failed");
    } finally {
      setUninstallBusy(false);
    }
  };

  return (
    <InfoCard title="Helm Release">
      {queuedMessage ? <p className="banner-message" role="status">{queuedMessage}</p> : null}
      <DetailRow label="Release Name" value={data.releaseName || summary?.releaseName} />
      <DetailRow label="Chart" value={`${data.chartName || summary?.chartName || "—"} ${data.chartVersion || summary?.chartVersion || ""}`.trim()} />
      <DetailRow label="App Version" value={data.appVersion || summary?.appVersion} />
      <DetailRow label="Revision" value={data.revision ?? summary?.helmRevision} />
      <DetailRow label="Status" value={data.status || summary?.helmStatus} />
      <DetailRow label="Last Deployed" value={formatDeployedAt(data.lastDeployed || summary?.lastDeployed)} />
      <DetailRow label="Namespace" value={data.namespace || summary?.namespace} />

      {valuesKeys.length ? (
        <>
          <h4 className="eyebrow">Values Summary</h4>
          <pre className="yaml-preview">{JSON.stringify(data.valuesSummary, null, 2)}</pre>
        </>
      ) : null}

      <div className="inventory-detail-actions">
        {/* Shown only when the host page has a manifest view to open. */}
        {canViewHelm && onViewManifest ? (
          <button type="button" className="btn-outline" onClick={onViewManifest}>
            View Manifest
          </button>
        ) : null}
        {canUpgradeHelm ? (
          <button type="button" className="btn-outline" onClick={onUpgrade}>
            Upgrade
          </button>
        ) : null}
        {canRollbackHelm ? (
          <button type="button" className="btn-outline" onClick={() => setRollbackOpen(true)}>
            Rollback
          </button>
        ) : null}
        {canUninstallHelm ? (
          <button type="button" className="btn-outline btn-danger-outline" onClick={() => setUninstallOpen(true)}>
            Uninstall
          </button>
        ) : null}
      </div>

      {data.renderedManifest && canViewHelm ? (
        <>
          <h4 className="eyebrow">Rendered Resources</h4>
          <pre className="yaml-preview">{data.renderedManifest}</pre>
        </>
      ) : null}

      <ConfirmActionModal
        open={rollbackOpen}
        title="Rollback Helm release"
        message={`Roll back ${data.releaseName || summary?.applicationName} to the previous chart revision?`}
        confirmLabel="Rollback"
        danger
        busy={rollbackBusy}
        error={rollbackError}
        onClose={() => {
          if (!rollbackBusy) {
            setRollbackOpen(false);
            setRollbackError("");
          }
        }}
        onConfirm={handleRollback}
      />

      <ConfirmActionModal
        open={uninstallOpen}
        title="Uninstall Helm release"
        message={`Uninstall ${data.releaseName || summary?.applicationName}? This removes every Kubernetes resource the release created.`}
        confirmLabel="Uninstall"
        danger
        busy={uninstallBusy}
        error={uninstallError}
        onClose={() => {
          if (!uninstallBusy) {
            setUninstallOpen(false);
            setUninstallError("");
          }
        }}
        onConfirm={handleUninstall}
      />
    </InfoCard>
  );
}
