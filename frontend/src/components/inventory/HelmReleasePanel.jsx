import { useState } from "react";
import InfoCard from "../common/InfoCard.jsx";
import ConfirmActionModal from "./ConfirmActionModal.jsx";
import { rollbackHelmRelease, uninstallHelmRelease } from "../../api/helmApi.js";

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

  if (!helm && summary?.source !== "Helm") {
    return null;
  }

  const data = helm || {};
  const valuesKeys = Object.keys(data.valuesSummary || {});

  const handleRollback = async () => {
    setRollbackBusy(true);
    setRollbackError("");
    try {
      await rollbackHelmRelease({
        clusterId: summary.cluster,
        namespace: summary.namespace,
        releaseName: data.releaseName || summary.applicationName,
      });
      setRollbackOpen(false);
      onActionComplete?.();
    } catch (err) {
      setRollbackError(err.message || "Rollback failed");
    } finally {
      setRollbackBusy(false);
    }
  };

  const handleUninstall = async () => {
    if (
      !window.confirm(
        `Uninstall Helm release ${data.releaseName || summary?.applicationName}? This removes Kubernetes resources.`
      )
    ) {
      return;
    }
    try {
      await uninstallHelmRelease({
        clusterId: summary.cluster,
        namespace: summary.namespace,
        releaseName: data.releaseName || summary.applicationName,
      });
      onActionComplete?.();
    } catch (err) {
      window.alert(err.message || "Uninstall failed");
    }
  };

  return (
    <InfoCard title="Helm Release">
      <DetailRow label="Release Name" value={data.releaseName || summary?.releaseName} />
      <DetailRow label="Chart" value={`${data.chartName || summary?.chartName || "—"} ${data.chartVersion || summary?.chartVersion || ""}`.trim()} />
      <DetailRow label="App Version" value={data.appVersion || summary?.appVersion} />
      <DetailRow label="Revision" value={data.revision ?? summary?.helmRevision} />
      <DetailRow label="Status" value={data.status || summary?.helmStatus} />
      <DetailRow label="Last Deployed" value={data.lastDeployed || summary?.lastDeployed} />
      <DetailRow label="Namespace" value={data.namespace || summary?.namespace} />

      {valuesKeys.length ? (
        <>
          <h4 className="eyebrow">Values Summary</h4>
          <pre className="yaml-preview">{JSON.stringify(data.valuesSummary, null, 2)}</pre>
        </>
      ) : null}

      <div className="inventory-detail-actions">
        {canViewHelm ? (
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
          <button type="button" className="btn-outline btn-danger-outline" onClick={handleUninstall}>
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
    </InfoCard>
  );
}
