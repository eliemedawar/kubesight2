import { useEffect, useRef, useState } from "react";

/**
 * Modal for requesting a deployment or change in a cluster. The request is
 * routed to the management team by email with approve/decline actions.
 */
export default function RequestDeploymentModal({
  open,
  clusterName,
  requesterName,
  busy = false,
  error = "",
  onClose,
  onSubmit,
}) {
  const [message, setMessage] = useState("");
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const textareaRef = useRef(null);

  const timeZone =
    Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";

  const pad = (n) => String(n).padStart(2, "0");
  const toLocalInput = (d) =>
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
      d.getHours()
    )}:${pad(d.getMinutes())}`;
  const nowLocal = toLocalInput(new Date());

  useEffect(() => {
    if (open) {
      setMessage("");
      setWindowStart("");
      setWindowEnd("");
      // Focus the textarea once the modal is mounted.
      const id = requestAnimationFrame(() => textareaRef.current?.focus());
      return () => cancelAnimationFrame(id);
    }
    return undefined;
  }, [open]);

  if (!open) return null;

  const trimmed = message.trim();
  const missingWindow = !windowStart || !windowEnd;
  const startInPast = windowStart && new Date(windowStart) <= new Date();
  const endBeforeStart =
    windowStart && windowEnd && new Date(windowEnd) <= new Date(windowStart);
  const windowError = startInPast
    ? "The start time must be in the future."
    : endBeforeStart
      ? "The end time must be after the start time."
      : "";
  const canSubmit = Boolean(trimmed) && !busy && !missingWindow && !windowError;

  const handleSubmit = (event) => {
    event.preventDefault();
    if (!canSubmit) return;
    // datetime-local values are wall-clock in the user's timezone; convert to
    // UTC ISO and send the IANA zone so approvers see the original local time.
    const payload = {
      message: trimmed,
      windowStart: new Date(windowStart).toISOString(),
      windowEnd: new Date(windowEnd).toISOString(),
      windowTimezone: timeZone,
    };
    onSubmit(payload);
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Request deployment or change"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <h3>Request Deployment / Change</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" /></svg>
          </button>
        </header>

        <form onSubmit={handleSubmit}>
          <div className="form-grid" style={{ marginBottom: "var(--space-3)" }}>
            <label className="form-grid__full">
              Cluster
              <input type="text" value={clusterName || ""} readOnly disabled />
            </label>
            <label className="form-grid__full">
              Requested by
              <input type="text" value={requesterName || ""} readOnly disabled />
            </label>
            <label className="form-grid__full">
              Describe what you want to deploy or change
              <textarea
                ref={textareaRef}
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                rows={5}
                maxLength={5000}
                placeholder="e.g. Deploy nginx with 3 replicas in the payments namespace"
                required
              />
            </label>
            <label>
              Work window start
              <input
                type="datetime-local"
                value={windowStart}
                min={nowLocal}
                onChange={(e) => setWindowStart(e.target.value)}
                required
              />
            </label>
            <label>
              Work window end
              <input
                type="datetime-local"
                value={windowEnd}
                min={windowStart || nowLocal}
                onChange={(e) => setWindowEnd(e.target.value)}
                required
              />
            </label>
            <p className="form-grid__full muted" style={{ margin: 0, fontSize: "0.8rem" }}>
              Times are in your timezone ({timeZone}). The request must be approved
              before the start time — otherwise it is automatically declined.
            </p>
          </div>

          {windowError ? <p className="banner-message error">{windowError}</p> : null}
          {error ? <p className="banner-message error">{error}</p> : null}

          <div className="modal-actions">
            <button type="button" className="btn-text" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={!canSubmit}>
              {busy ? "Sending…" : "Send Request"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
