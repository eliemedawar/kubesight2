import { useEffect, useRef } from "react";
import { parseAlertTime } from "../../lib/alertFeed.js";
import { useEscapeToClose } from "../../hooks/useEscapeToClose.js";

// Backend timestamps are naive UTC; parseAlertTime reads them as UTC so the
// shown time is the viewer's local time, not off by their UTC offset.
function formatTimestamp(value) {
  if (!value) {
    return "—";
  }
  const ts = parseAlertTime(value);
  return Number.isNaN(ts) ? String(value) : new Date(ts).toLocaleString();
}

export default function AlertLogContextModal({ open, alert, deliveryLog, onClose }) {
  const closeRef = useRef(null);
  // Opens over the alert drawer, so it sits on top of the shared stack and
  // Escape closes only this modal.
  useEscapeToClose(onClose, true, Boolean(open));

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const previousFocus = document.activeElement;
    closeRef.current?.focus();
    return () => {
      previousFocus?.focus?.();
    };
  }, [open]);

  if (!open) {
    return null;
  }

  const isDelivery = Boolean(deliveryLog);
  const payload = isDelivery ? deliveryLog : alert;
  const logSnippet = payload?.logSnippet || (payload?.logLines || []).join("\n");
  const title = isDelivery ? "Delivered Log Snippet" : "Log Alert Details";

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="modal-card alert-log-detail-modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header className="modal-header">
          <h2>{title}</h2>
          <button type="button" ref={closeRef} className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" /></svg>
          </button>
        </header>

        <dl className="alert-log-detail-grid">
          {!isDelivery ? (
            <>
              <div>
                <dt>Cluster</dt>
                <dd>{alert?.clusterId || "—"}</dd>
              </div>
              <div>
                <dt>Namespace</dt>
                <dd>{alert?.namespace || "—"}</dd>
              </div>
              <div>
                <dt>Resource</dt>
                <dd>{alert?.deployment || alert?.resourceName || "—"}</dd>
              </div>
              <div>
                <dt>Pod</dt>
                <dd>{alert?.pod || "—"}</dd>
              </div>
              <div>
                <dt>Container</dt>
                <dd>{alert?.container || "—"}</dd>
              </div>
            </>
          ) : (
            <>
              <div>
                <dt>Policy</dt>
                <dd>{deliveryLog?.policyName || "—"}</dd>
              </div>
              <div>
                <dt>Receiver</dt>
                <dd>{deliveryLog?.receiverName || "—"}</dd>
              </div>
              <div>
                <dt>Pod</dt>
                <dd>{deliveryLog?.podName || "—"}</dd>
              </div>
            </>
          )}
          <div>
            <dt>Matched Pattern</dt>
            <dd>{payload?.matchedPattern || "—"}</dd>
          </div>
          <div>
            <dt>Timestamp</dt>
            <dd>{formatTimestamp(payload?.detectedAt || payload?.deliveredAt || alert?.firedAt)}</dd>
          </div>
        </dl>

        <section className="alert-log-snippet-section">
          <h3>Log Snippet</h3>
          <pre className="alert-log-snippet">{logSnippet || "No log content captured."}</pre>
        </section>

        <footer className="modal-footer">
          <button type="button" className="btn-outline" onClick={onClose}>
            Close
          </button>
        </footer>
      </div>
    </div>
  );
}
