/** Day two: copying workloads into a cluster that is already running.
 *
 *  The same picker the wizard uses, plus the one thing day two adds: the
 *  cluster is live, so the copy is applied on its own rather than as part of a
 *  build. It reuses the build's phase machine — every other phase is already
 *  completed and skipped, so only 'workloads' runs.
 */

import { useState } from "react";
import WorkloadsPicker from "./WorkloadsPicker.jsx";
import {
  emptyStorage,
  storageErrors,
  storageRows,
  workloadPlanVerdict,
  workloadSelectionSummary,
} from "../../utils/clusterBuilder.js";
import {
  bringClusterWorkloads,
  setBuildWorkloads,
} from "../../api/clusterBuildsApi.js";

function initialValue(build) {
  const selection = build?.workloadSelection || {};
  const storage = selection.storage || {};
  return {
    sourceClusterId: selection.sourceClusterId || "",
    sourceClusterName: selection.sourceClusterName || "",
    registryConnectionId: selection.registryConnectionId || null,
    storage: {
      ...emptyStorage(),
      ...storage,
      // Stored as a list, edited as one comma-separated field.
      nfsMountOptions: Array.isArray(storage.nfsMountOptions)
        ? storage.nfsMountOptions.join(",")
        : storage.nfsMountOptions || "",
    },
    // Only the pending selection, never the applied history.
    items: selection.items || [],
  };
}

export default function WorkloadsPanel({
  build, canExecute, notify, onChanged, onClose,
}) {
  const [value, setValue] = useState(() => initialValue(build));
  const [plan, setPlan] = useState(null);
  const [acked, setAcked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const verdict = plan ? workloadPlanVerdict(plan) : null;
  const missingCount = verdict?.missing.length || 0;
  const needsAck = missingCount > 0;
  // A claim with no valid destination is refused by the backend too; catching
  // it here means the reason is next to the row that caused it.
  const volumeErrors = storageErrors(storageRows(plan, value.storage));
  const canApply = Boolean(
    value.items.length && canExecute && !busy && !volumeErrors.length
    && (!needsAck || acked)
  );

  const apply = async () => {
    setBusy(true);
    setError("");
    try {
      // Save first: the selection is what the phase reads, and saving it means
      // a failed copy can be retried from the build without re-picking.
      await setBuildWorkloads(build.id, value);
      await bringClusterWorkloads(build.id, { ackMissingImages: needsAck });
      notify(
        `Copying ${workloadSelectionSummary(value.items, { short: true }).toLowerCase()}`
        + ` into ${build.name}.`
      );
      onChanged();
      onClose();
    } catch (exception) {
      // Inline as well as page-level: this panel sits below the fold.
      const message = exception.message || String(exception);
      setError(message);
      notify(message, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card sg-cb-grow">
      <div className="sg-cb-sect">
        <h2>Bring workloads over</h2>
        <span className="sg-cb-sect-right">
          <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
        </span>
      </div>
      {error ? <p className="sg-cb-grow-error" role="alert">{error}</p> : null}

      <p className="muted sg-cb-grow-lede">
        Copies namespaces or individual workloads out of another cluster and applies
        them here. The source cluster is only read — nothing there is moved, scaled
        or deleted. Objects that already exist in this cluster are updated in place,
        so running this twice is safe.
      </p>

      <WorkloadsPicker
        value={value}
        onChange={(next) => { setValue(next); setAcked(false); }}
        onPlanChange={(next) => { setPlan(next); setAcked(false); }}
        notify={notify}
        compact
      />

      {needsAck ? (
        <div className="sg-cb-ackbar">
          <label>
            <input
              type="checkbox"
              checked={acked}
              onChange={(event) => setAcked(event.target.checked)}
            />
            {missingCount === 1
              ? "Copy the workload with no image in that registry anyway — its pod "
                + "will wait in ImagePullBackOff."
              : `Copy the ${missingCount} workloads with no image in that registry `
                + "anyway — their pods will wait in ImagePullBackOff."}
          </label>
        </div>
      ) : null}

      <div className="sg-cb-actions">
        {volumeErrors.length ? (
          <span className="sg-cb-field-error">{volumeErrors[0]}</span>
        ) : null}
        <button className="primary" type="button" disabled={!canApply} onClick={apply}>
          {busy
            ? "Copying…"
            : value.items.length
              ? `Copy ${workloadSelectionSummary(value.items, { short: true }).toLowerCase()}`
                + ` into ${build.name}`
              : "Select something to copy"}
        </button>
        {!canExecute ? (
          <span className="muted">
            Applying workloads needs the build-execute permission.
          </span>
        ) : null}
      </div>
    </div>
  );
}
