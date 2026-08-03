/**
 * Status vocabulary, shared.
 *
 * The four integration states from contract 2 are the canonical set, and they
 * generalise: something is working, working badly, deliberately off, or never
 * set up. Pages had been inventing their own words for these — "Configured,
 * off", "Last sync failed", "Not configured" — which meant an operator scanning
 * two screens had to work out that two different phrasings meant the same
 * thing.
 *
 * `unknown` exists for the case contract 2 calls an unavailable card: the
 * backend could not determine the state. That is genuinely different from
 * `not_configured` and must not be collapsed into it.
 */

export const STATUS = {
  CONNECTED: "connected",
  DEGRADED: "degraded",
  DISABLED: "disabled",
  NOT_CONFIGURED: "not_configured",
  UNKNOWN: "unknown",
};

const PRESENTATION = {
  [STATUS.CONNECTED]: { label: "Connected", tone: "ok" },
  [STATUS.DEGRADED]: { label: "Degraded", tone: "warn" },
  [STATUS.DISABLED]: { label: "Disabled", tone: "muted" },
  [STATUS.NOT_CONFIGURED]: { label: "Not configured", tone: "muted" },
  [STATUS.UNKNOWN]: { label: "Status unavailable", tone: "muted" },
};

export function statusPresentation(status) {
  return PRESENTATION[status] || PRESENTATION[STATUS.UNKNOWN];
}

export default function StatusPill({ status, label, className = "" }) {
  const presentation = statusPresentation(status);
  return (
    <span
      className={`status-pill status-pill--${presentation.tone} ${className}`.trim()}
      data-status={status}
    >
      {label || presentation.label}
    </span>
  );
}
