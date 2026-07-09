import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  formatAlertTime,
  getAlertResourceName,
  getAlertTypeLabel,
  isLogAlert,
  isServiceAlert,
} from "../../lib/alertDisplay.js";
import { formatDurationShort, formatFiringDuration, parseAlertTime, severityInfo } from "../../lib/alertFeed.js";

const AlertLogContextModal = lazy(() => import("./AlertLogContextModal.jsx"));

function numeric(value) {
  const num = Number.parseFloat(String(value ?? "").replace("%", ""));
  return Number.isFinite(num) ? num : null;
}

function overBy(condition) {
  const observed = numeric(condition.observedValue);
  const threshold = numeric(condition.threshold);
  if (observed == null || threshold == null) {
    return condition.matched === false ? "not met" : "met";
  }
  const delta = observed - threshold;
  const rounded = Math.round(delta * 100) / 100;
  return `${rounded > 0 ? "+" : ""}${rounded}`;
}

function ConditionsTable({ conditions }) {
  return (
    <div className="al-cond">
      <table>
        <thead>
          <tr>
            <th>Condition</th>
            <th>Threshold</th>
            <th>Observed</th>
            <th>Over by</th>
          </tr>
        </thead>
        <tbody>
          {conditions.map((condition, index) => (
            <tr key={index} className={condition.matched === false ? "al-cond--miss" : undefined}>
              <td>{condition.metricLabel || condition.metricKey || "metric"}</td>
              <td className="al-mono">{`${condition.operator || ""} ${condition.threshold ?? "—"}`.trim()}</td>
              <td className="al-mono">{condition.observedValue ?? "—"}</td>
              <td className={`al-mono${condition.matched !== false ? " al-over" : ""}`}>{overBy(condition)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LogSection({ alert }) {
  const [modalOpen, setModalOpen] = useState(false);
  const lines = (alert.logLines || []).length
    ? alert.logLines
    : String(alert.logSnippet || "").split("\n").filter(Boolean);
  const pattern = String(alert.matchedPattern || "");
  const patternLower = pattern.toLowerCase();

  return (
    <>
      <section className="al-dr-sec">
        <h5>Matched pattern</h5>
        <dl className="al-kv">
          <dt>Pattern</dt>
          <dd><span className="al-chip">{pattern || "—"}</span></dd>
          {alert.detectedAt ? (
            <>
              <dt>Detected</dt>
              <dd>{formatAlertTime(alert.detectedAt)}</dd>
            </>
          ) : null}
        </dl>
      </section>
      {lines.length ? (
        <section className="al-dr-sec">
          <h5>Log context</h5>
          <div className="al-logwell">
            {lines.slice(0, 12).map((line, index) => (
              <span
                key={index}
                className={`al-ln${patternLower && String(line).toLowerCase().includes(patternLower) ? " al-ln--hit" : ""}`}
              >
                {line}
              </span>
            ))}
          </div>
          <button type="button" className="btn-outline al-dr-logbtn" onClick={() => setModalOpen(true)}>
            Open full context
          </button>
        </section>
      ) : null}
      {modalOpen ? (
        <Suspense fallback={null}>
          <AlertLogContextModal open alert={alert} onClose={() => setModalOpen(false)} />
        </Suspense>
      ) : null}
    </>
  );
}

function ServiceSection({ alert }) {
  return (
    <>
      <section className="al-dr-sec">
        <h5>Service</h5>
        <dl className="al-kv">
          {alert.serviceName ? (
            <>
              <dt>Service</dt>
              <dd>{alert.serviceName}</dd>
            </>
          ) : null}
          <dt>Component</dt>
          <dd className="al-mono">{getAlertResourceName(alert)}</dd>
        </dl>
        {alert.description ? <p className="al-dr-desc">{alert.description}</p> : null}
      </section>
      {(alert.affectedClients || []).length ? (
        <section className="al-dr-sec">
          <h5>Affected clients</h5>
          <div className="al-chips">
            {alert.affectedClients.map((client) => (
              <span key={client} className="al-chip">{client}</span>
            ))}
          </div>
        </section>
      ) : null}
    </>
  );
}

/**
 * Slide-over detail drawer for a single alert: why it fired, log/service
 * context, and scope. Works for both firing feed rows and resolved history
 * rows (which carry status/resolvedAt).
 */
export default function AlertDetailDrawer({ alert, clusterLabel, onClose, onViewPolicy }) {
  const closeRef = useRef(null);
  const previousFocusRef = useRef(null);

  useEffect(() => {
    if (!alert) {
      return undefined;
    }
    previousFocusRef.current = document.activeElement;
    closeRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previousFocusRef.current?.focus) {
        previousFocusRef.current.focus();
      }
    };
  }, [alert, onClose]);

  if (!alert) {
    return null;
  }

  const severity = severityInfo(alert.severity);
  const resolved = String(alert.status || "").toLowerCase() === "resolved";
  const duration = resolved
    ? (() => {
        const firedTs = parseAlertTime(alert.firedAt);
        const resolvedTs = parseAlertTime(alert.resolvedAt);
        return !Number.isNaN(firedTs) && !Number.isNaN(resolvedTs) && resolvedTs >= firedTs
          ? formatDurationShort(resolvedTs - firedTs)
          : "";
      })()
    : formatFiringDuration(alert.firedAt);
  const conditions = Array.isArray(alert.triggeredConditions) ? alert.triggeredConditions : [];
  const showConditions = !isLogAlert(alert) && !isServiceAlert(alert) && conditions.length > 0;

  return (
    <div className="al-drawer-root" role="presentation">
      <div className="al-scrim" onClick={onClose} />
      <aside className="al-drawer" role="dialog" aria-modal="true" aria-label="Alert details">
        <div className="al-dr-head">
          <div className="al-dr-top">
            <span className={`status-pill ${resolved ? "ok" : severity.tone}`}>
              {resolved ? "resolved" : severity.label}
            </span>
            <span className="al-type">{getAlertTypeLabel(alert)}</span>
            {/* icon-button opts out of the global padded-pill button rule,
                whose padding otherwise crushes the icon's content box to 0. */}
            <button
              type="button"
              ref={closeRef}
              className="al-dr-close icon-button"
              onClick={onClose}
              aria-label="Close alert details"
            >
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </div>
          <h4>{alert.title || alert.policyName || "Alert"}</h4>
          <p className="al-dr-meta">
            fired <b>{formatAlertTime(alert.firedAt)}</b>
            {resolved
              ? ` · resolved ${formatAlertTime(alert.resolvedAt)}${duration ? ` after ${duration}` : ""}`
              : duration
                ? ` · firing ${duration}`
                : ""}
          </p>
        </div>

        <div className="al-dr-body">
          {showConditions ? (
            <section className="al-dr-sec">
              <h5>Why it fired</h5>
              <ConditionsTable conditions={conditions} />
            </section>
          ) : null}
          {!showConditions && !isLogAlert(alert) && !isServiceAlert(alert) && alert.description ? (
            <section className="al-dr-sec">
              <h5>Why it fired</h5>
              <p className="al-dr-desc">{alert.description}</p>
            </section>
          ) : null}

          {isLogAlert(alert) ? <LogSection alert={alert} /> : null}
          {isServiceAlert(alert) ? <ServiceSection alert={alert} /> : null}

          <section className="al-dr-sec">
            <h5>Scope</h5>
            <dl className="al-kv">
              <dt>Cluster</dt>
              <dd className="al-mono">{clusterLabel || alert.clusterId || "—"}</dd>
              {alert.namespace ? (
                <>
                  <dt>Namespace</dt>
                  <dd className="al-mono">ns/{alert.namespace}</dd>
                </>
              ) : null}
              <dt>Resource</dt>
              <dd className="al-mono">{getAlertResourceName(alert)}</dd>
              {alert.policyName ? (
                <>
                  <dt>Policy</dt>
                  <dd>{alert.policyName}</dd>
                </>
              ) : null}
            </dl>
          </section>
        </div>

        <div className="al-dr-foot">
          {alert.policyId != null && onViewPolicy ? (
            <button type="button" className="btn-outline" onClick={() => onViewPolicy(alert)}>
              View policy
            </button>
          ) : null}
          <button type="button" className="btn-ghost" onClick={onClose}>
            Close
          </button>
        </div>
      </aside>
    </div>
  );
}
