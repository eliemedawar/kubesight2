import { useEffect, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import ZohoLayoutDiff from "./ZohoLayoutDiff.jsx";
import { useTicketing } from "../ticketing/TicketingContext.jsx";

const reasonLabel = (reason) =>
  String(reason || "layout_write")
    .replace(/^before_restore_/, "Before restore #")
    .replaceAll("_", " ");

const timestamp = (value) => (value ? new Date(value).toLocaleString() : "Unknown time");

export default function ZohoRecoveryModal({ canManage, onClose, onRestored }) {
  const { api } = useTicketing();
  const [snapshots, setSnapshots] = useState([]);
  const [selected, setSelected] = useState(null);
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    api
      .listLayoutSnapshots(10)
      .then((result) => {
        if (active) setSnapshots(result?.items || []);
      })
      .catch((err) => {
        if (active) setError(err.message || "Could not load recovery snapshots.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [api]);

  const preview = async (snapshot) => {
    setSelected(snapshot);
    setPlan(null);
    setBusy(true);
    setError("");
    try {
      setPlan(await api.planLayoutSnapshotRestore(snapshot.id));
    } catch (err) {
      setError(err.message || "Could not preview this recovery snapshot.");
    } finally {
      setBusy(false);
    }
  };

  const restore = async () => {
    setBusy(true);
    setError("");
    try {
      await api.restoreLayoutSnapshot(selected.id);
      onRestored(`Layout restored from the ${timestamp(selected.takenAt)} snapshot.`);
    } catch (err) {
      setError(err.message || "Could not restore this layout snapshot.");
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-zh-modal--recovery"
        role="dialog"
        aria-modal="true"
        aria-label="Layout recovery"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3>Layout recovery</h3>
            <p className="muted">Pre-write snapshots are kept for the last 10 layout changes.</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        {error ? <ErrorBanner message={error} /> : null}

        {plan ? (
          <>
            <div className="sg-zh-recovery-selected">
              <b>{timestamp(selected?.takenAt)}</b>
              <span>{reasonLabel(selected?.reason)}</span>
              {selected?.actor ? <span>by {selected.actor}</span> : null}
            </div>
            <ZohoLayoutDiff plan={plan} />
            {!canManage ? (
              <p className="muted">You can inspect snapshots, but restoring requires manage access.</p>
            ) : null}
            <div className="modal-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => {
                  setPlan(null);
                  setSelected(null);
                  setError("");
                }}
              >
                Back to snapshots
              </button>
              <button
                type="button"
                className="btn-danger"
                onClick={restore}
                disabled={busy || !canManage || !plan.writesEnabled}
              >
                {busy ? "Restoring…" : "Restore this snapshot"}
              </button>
            </div>
          </>
        ) : loading ? (
          <p className="muted">Loading recovery snapshots…</p>
        ) : snapshots.length === 0 ? (
          <EmptyState message="No recovery snapshots yet. One is created automatically before each structural layout change." />
        ) : (
          <div className="sg-zh-recovery-list">
            {snapshots.map((snapshot, index) => (
              <button
                key={snapshot.id}
                type="button"
                className="sg-zh-recovery-row"
                onClick={() => preview(snapshot)}
                disabled={busy}
              >
                <span>
                  <b>{timestamp(snapshot.takenAt)}</b>
                  <small>
                    {reasonLabel(snapshot.reason)}
                    {snapshot.actor ? ` · ${snapshot.actor}` : ""}
                  </small>
                </span>
                <span className="sg-zh-recovery-counts">
                  {snapshot.sectionCount} sections · {snapshot.fieldCount} fields
                </span>
                <span className="sg-zh-recovery-view">
                  {busy && selected?.id === snapshot.id ? "Checking…" : index === 0 ? "View latest" : "View"}
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
