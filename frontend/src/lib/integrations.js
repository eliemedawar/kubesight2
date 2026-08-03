/**
 * The integration contract, frontend side.
 *
 * Every integration — a ticketing platform, an SMTP server, a registry, an LLM
 * service — is described by one provider-neutral shape, so the hub renders a
 * card and a detail screen without knowing what any particular provider is:
 *
 *   {
 *     key, name, category, status, enabled,
 *     lastTestedAt, lastSuccessfulSyncAt, message,
 *     capabilities: [...], usedBy: [...], actions: [...]
 *   }
 *
 * Adding a provider is a backend adapter plus, if it needs one, a configuration
 * panel. Nothing else in the hub changes.
 */

/**
 * The only four states we show an operator.
 *
 * Internally an integration can fail a dozen ways — auth rejected, host
 * unreachable, TLS invalid, quota exceeded. None of that belongs on a card. It
 * collapses to: is it working, is it working badly, did someone turn it off, or
 * was it never set up. The specifics go in `message` on the detail screen.
 */
export const INTEGRATION_STATUS = {
  connected: {
    id: "connected",
    label: "Connected",
    tone: "ok",
    hint: "Working — last check succeeded.",
  },
  degraded: {
    id: "degraded",
    label: "Degraded",
    tone: "warn",
    hint: "Configured and on, but the last check or sync failed.",
  },
  disabled: {
    id: "disabled",
    label: "Disabled",
    tone: "muted",
    hint: "Configured, but switched off — nothing runs.",
  },
  not_configured: {
    id: "not_configured",
    label: "Not configured",
    tone: "muted",
    hint: "No connection details saved yet.",
  },
};

/** Unknown states degrade to "Not configured" rather than rendering blank. */
export function statusMeta(status) {
  return INTEGRATION_STATUS[status] || INTEGRATION_STATUS.not_configured;
}

/** Category render order. Anything unrecognised sorts to the end, alphabetically. */
export const CATEGORY_ORDER = [
  "Ticketing",
  "CI/CD",
  "Notifications",
  "Artifacts",
  "Source control",
  "Intelligence",
];

export function groupByCategory(items = []) {
  const byCategory = new Map();
  items.forEach((item) => {
    const category = item.category || "Other";
    if (!byCategory.has(category)) {
      byCategory.set(category, []);
    }
    byCategory.get(category).push(item);
  });
  return [...byCategory.entries()]
    .map(([category, entries]) => ({ category, items: entries }))
    .sort((a, b) => {
      const ai = CATEGORY_ORDER.indexOf(a.category);
      const bi = CATEGORY_ORDER.indexOf(b.category);
      if (ai === -1 && bi === -1) return a.category.localeCompare(b.category);
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
}

/** Detail-screen tabs. Every integration gets all four; empty ones say so. */
export const DETAIL_TABS = [
  { key: "overview", label: "Overview" },
  { key: "configuration", label: "Configuration" },
  { key: "activity", label: "Activity" },
  { key: "usedBy", label: "Used by" },
];

/** Capability slugs are stable ids; these are what an operator reads. */
const CAPABILITY_LABELS = {
  "ticket-sync": "Ticket sync",
  "deployment-approval": "Deployment approval",
  "deploy-trigger": "Deploy trigger",
  "build-status": "Build status",
  "email-delivery": "Email delivery",
  "chat-notify": "Chat notifications",
  "webhook-notify": "Outbound webhooks",
  "image-verify": "Image verification",
  "repo-read": "Repository access",
  "pull-request": "Pull requests",
  "llm-analysis": "AI analysis",
};

export function capabilityLabel(slug) {
  return CAPABILITY_LABELS[slug] || slug;
}

/** Action slugs the backend may offer on an integration. */
export const ACTION_LABELS = {
  configure: "Configure",
  test: "Test connection",
  enable: "Enable",
  disable: "Disable",
};

export function hasAction(integration, action) {
  return Array.isArray(integration?.actions) && integration.actions.includes(action);
}

/**
 * Absolute timestamps, formatted for a person. The backend serializes naive UTC
 * without a Z suffix, so append one — otherwise the browser reads it as local
 * and quietly shifts every timestamp by the viewer's offset.
 */
export function formatTimestamp(isoValue) {
  if (!isoValue) return "Never";
  const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(isoValue) ? isoValue : `${isoValue}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return isoValue;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Relative time now lives in lib/relativeTime.js. This re-export keeps the
 * import path working for callers that already had it while the other four
 * hand-rolled copies in this tree converge on the same implementation — they
 * disagreed on rounding and, more seriously, on whether a naive backend
 * timestamp is UTC.
 */
export { relativeTime as timeAgo } from "./relativeTime.js";
