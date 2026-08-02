import { parseApiTime } from "../lib/apiTime.js";

/**
 * "What needs attention?", answered in priority order.
 *
 * The dashboard used to be a wall of charts and counters that reported state
 * without ranking it: 3 critical alerts and a degraded Jira connection got the
 * same visual weight as a namespace count. An operator opening it at the start
 * of a shift had to assemble the priority list themselves, from several panels,
 * every time.
 *
 * This composes one ranked list from sources the app already has. Every item
 * carries the six things needed to act on it without opening anything else:
 * severity, scope, when it was detected, who owns it, the recommended action,
 * and a link straight to it.
 *
 * Deliberately additive and honest. A source that is unavailable contributes
 * nothing rather than a placeholder — the feed being short because integrations
 * failed to load is different from there being nothing wrong, and
 * `unavailableSources` lets the caller say which happened. Nothing here invents
 * severity or timing it was not given.
 */

export const SEVERITY = { CRITICAL: "critical", WARNING: "warning", INFO: "info" };

const SEVERITY_RANK = { critical: 0, warning: 1, info: 2 };

/**
 * Ranking. Severity first, then recency: two critical items are ordered by
 * which started more recently, because the newest one is the one that just
 * changed and is least likely to have been seen already.
 *
 * Items with no timestamp sort after ones that have it at the same severity —
 * "we do not know when this started" is weaker evidence than a known time, and
 * treating a missing date as "now" would float it to the top of every list.
 */
export function compareAttention(a, b) {
  const rank = (SEVERITY_RANK[a.severity] ?? 2) - (SEVERITY_RANK[b.severity] ?? 2);
  if (rank !== 0) {
    return rank;
  }
  const at = parseApiTime(a.detectedAt);
  const bt = parseApiTime(b.detectedAt);
  const aKnown = Number.isFinite(at);
  const bKnown = Number.isFinite(bt);
  if (aKnown && bKnown) {
    return bt - at;
  }
  if (aKnown !== bKnown) {
    return aKnown ? -1 : 1;
  }
  return String(a.title).localeCompare(String(b.title));
}

function item({ id, severity, title, detail, scope, detectedAt, owner, action, href, source }) {
  return {
    id,
    severity,
    title,
    detail: detail || "",
    scope: scope || "",
    detectedAt: detectedAt || null,
    owner: owner || "",
    action: action || "",
    href: href || null,
    source,
  };
}

/* ── Sources ─────────────────────────────────────────────────────────── */

function fromAlerts(alerts, { clusterId }) {
  return (alerts || [])
    .filter((alert) => {
      const severity = String(alert.severity || "").toLowerCase();
      return severity === "critical" || severity === "warning";
    })
    .map((alert) =>
      item({
        id: `alert:${alert.id ?? alert.name}`,
        severity: String(alert.severity).toLowerCase(),
        title: alert.name || alert.message || "Alert firing",
        detail: alert.message && alert.message !== alert.name ? alert.message : "",
        scope: [alert.namespace, alert.pod || alert.resourceName].filter(Boolean).join(" / "),
        detectedAt: alert.firedAt || alert.createdAt || alert.startedAt,
        owner: alert.owner || "",
        action: "Investigate the firing alert",
        href: "/alerts",
        source: "alerts",
      })
    );
}

function fromIntegrations(integrations) {
  return (integrations || [])
    .filter((entry) => entry.status === "degraded")
    .map((entry) =>
      item({
        id: `integration:${entry.key}`,
        // Degraded, not critical: an integration failing does not take the
        // cluster down, and ranking it alongside a firing critical alert would
        // make the top of the feed less trustworthy.
        severity: SEVERITY.WARNING,
        title: `${entry.name} is degraded`,
        detail: entry.message || "",
        scope: entry.category || "Integration",
        detectedAt: entry.lastTestedAt,
        owner: "",
        action: "Check the connection and retest",
        href: `/integrations/${entry.key}`,
        source: "integrations",
      })
    );
}

function fromApprovals(requests) {
  return (requests || [])
    .filter((request) => String(request.status || "").toLowerCase() === "pending")
    .map((request) =>
      item({
        id: `approval:${request.id}`,
        severity: SEVERITY.INFO,
        title: `Deployment request awaiting approval`,
        detail: request.applicationName || request.deploymentName || request.summary || "",
        scope: [request.cluster || request.clusterId, request.namespace]
          .filter(Boolean)
          .join(" / "),
        detectedAt: request.createdAt,
        owner: request.requestedBy || request.requesterName || "",
        action: "Review and approve or decline",
        href: "/changes/requests",
        source: "approvals",
      })
    );
}

function fromClusterHealth(summary) {
  if (!summary) {
    return [];
  }
  const results = [];
  const nodes = summary.nodes || {};
  const total = Number(nodes.total) || 0;
  const ready = Number(nodes.ready) || 0;
  const notReady = Math.max(total - ready, 0);

  if (total > 0 && notReady > 0) {
    results.push(
      item({
        id: `nodes:${summary.clusterId}`,
        // Every node down is an outage; some nodes down is degraded capacity.
        severity: ready === 0 ? SEVERITY.CRITICAL : SEVERITY.WARNING,
        title: `${notReady} of ${total} nodes not ready`,
        detail: ready === 0 ? "No nodes are reporting ready." : "",
        scope: summary.clusterId || "",
        detectedAt: summary.lastUpdated,
        owner: "",
        action: "Inspect node conditions",
        href: summary.clusterId ? `/fleet/clusters/${summary.clusterId}` : "/fleet/clusters",
        source: "clusters",
      })
    );
  }

  return results;
}

function fromUpgradeRisk(summary) {
  const version = summary?.version;
  if (!version || version.status === "up_to_date" || !version.status) {
    return [];
  }
  // Only surface what the backend actually flagged. "Not up to date" is not by
  // itself urgent, and calling every minor version behind a risk would train
  // operators to ignore the feed.
  if (version.status !== "outdated" && version.status !== "unsupported") {
    return [];
  }
  return [
    item({
      id: `upgrade:${summary.clusterId}`,
      severity: version.status === "unsupported" ? SEVERITY.WARNING : SEVERITY.INFO,
      title:
        version.status === "unsupported"
          ? "Cluster is running an unsupported Kubernetes version"
          : "Kubernetes version is behind",
      detail: version.current ? `Currently ${version.current}` : "",
      scope: summary.clusterId || "",
      detectedAt: summary.lastUpdated,
      owner: "",
      action: "Run an upgrade precheck",
      href: "/fleet/upgrades",
      source: "upgrades",
    }),
  ];
}

/**
 * Compose the feed.
 *
 * Every source is optional. Pass `unavailable` for sources that failed to load
 * so the caller can distinguish a quiet system from a partly blind one.
 */
export function buildAttentionFeed({
  alerts,
  integrations,
  approvals,
  summary,
  clusterId,
  unavailable = [],
  limit = 0,
} = {}) {
  const items = [
    ...fromAlerts(alerts, { clusterId }),
    ...fromIntegrations(integrations),
    ...fromApprovals(approvals),
    ...fromClusterHealth(summary),
    ...fromUpgradeRisk(summary),
  ].sort(compareAttention);

  return {
    items: limit > 0 ? items.slice(0, limit) : items,
    total: items.length,
    counts: items.reduce((acc, entry) => {
      acc[entry.severity] = (acc[entry.severity] || 0) + 1;
      return acc;
    }, {}),
    unavailableSources: unavailable,
  };
}
