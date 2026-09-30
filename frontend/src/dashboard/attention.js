// What the dashboard puts in "Needs attention": the concrete problems on the
// active cluster, worst first, each pointing at the page where it is fixed.
// Pure functions over the dashboard summary so the ranking is testable.

const GIB = 1024 ** 3;

// Usage bars turn amber at the backend's disk/memory warning line
// (NODE_DISK_WARN_PERCENT in k8s_provider.py) and red at the kubelet's default
// eviction line (nodefs.available < 10%).
export const USAGE_WARN_PERCENT = 85;
export const USAGE_DANGER_PERCENT = 90;

const TONE_ORDER = { danger: 0, warn: 1, info: 2 };
const POD_ROWS = 3;
const ALERT_ROWS = 3;

export function formatBytes(bytes) {
  if (bytes == null || !Number.isFinite(Number(bytes))) return "—";
  const gib = Number(bytes) / GIB;
  if (gib >= 1024) return `${(gib / 1024).toFixed(1)} TiB`;
  if (gib >= 100) return `${Math.round(gib)} GiB`;
  return `${gib.toFixed(1)} GiB`;
}

export function usageTone(percent) {
  if (percent == null || !Number.isFinite(Number(percent))) return "muted";
  if (percent >= USAGE_DANGER_PERCENT) return "danger";
  if (percent >= USAGE_WARN_PERCENT) return "warn";
  return "ok";
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function nodeItems(nodes) {
  const items = [];
  for (const node of nodes || []) {
    const issues = [...(node.issues || [])];
    if (node.cordoned) issues.push("cordoned");
    if (node.status === "critical" || node.status === "warning") {
      items.push({
        id: `node:${node.name}`,
        tone: node.status === "critical" ? "danger" : "warn",
        kind: "Node",
        title: node.name,
        detail: issues.join(" · "),
        target: { page: "clusters" },
      });
    } else if (node.cordoned) {
      // Healthy but taking no new pods — usually maintenance, still less capacity.
      items.push({
        id: `node:${node.name}`,
        tone: "info",
        kind: "Node",
        title: node.name,
        detail: "Cordoned · no new pods are scheduled here",
        target: { page: "clusters" },
      });
    }
  }
  return items;
}

function podItems(summary) {
  const pods = summary?.problemPods || [];
  const total = Math.max(summary?.problemPodsTotal ?? pods.length, pods.length);
  if (!pods.length) {
    const failed = summary?.pods?.failed || 0;
    return failed
      ? [
          {
            id: "pods:failed",
            tone: "danger",
            kind: "Pods",
            title: `${plural(failed, "pod")} failing`,
            detail: "Open Resources to see which ones",
            target: { page: "resources", options: { params: { tab: "pods" } } },
          },
        ]
      : [];
  }
  const shown = total > POD_ROWS ? pods.slice(0, POD_ROWS - 1) : pods.slice(0, POD_ROWS);
  const items = shown.map((pod) => ({
    id: `pod:${pod.namespace}/${pod.name}`,
    tone: "danger",
    kind: "Pod",
    title: pod.name,
    detail: `${pod.status} · ${pod.namespace}`,
    target: {
      page: "resources",
      options: { params: { tab: "pods" }, query: pod.namespace ? { ns: pod.namespace } : undefined },
    },
  }));
  const rest = total - shown.length;
  if (rest > 0) {
    items.push({
      id: "pods:more",
      tone: "danger",
      kind: "Pods",
      title: `${plural(rest, "more pod")} failing`,
      detail: [...new Set(pods.slice(shown.length).map((p) => p.status))].join(", ") || "See Resources",
      target: { page: "resources", options: { params: { tab: "pods" } } },
    });
  }
  return items;
}

function alertItems(summary) {
  const top = summary?.topAlerts || [];
  const alertTarget = { page: "alerts", options: { params: { tab: "open" } } };
  const critical = top.filter((a) => a.severity === "critical").slice(0, ALERT_ROWS);
  const items = critical.map((alert) => ({
    id: `alert:${alert.id}`,
    tone: "danger",
    kind: "Alert",
    title: alert.title,
    detail: alert.namespace ? `Critical · ${alert.namespace}` : "Critical",
    target: alertTarget,
  }));
  const warnings = summary?.alerts?.warning || 0;
  if (warnings > 0) {
    const first = top.find((a) => a.severity === "warning");
    items.push({
      id: "alerts:warning",
      tone: "warn",
      kind: "Alerts",
      title: `${plural(warnings, "warning alert")}`,
      detail: first ? `Latest: ${first.title}` : "Open Alerts to triage",
      target: alertTarget,
    });
  }
  return items;
}

function versionItem(summary) {
  const version = summary?.version || {};
  if (version.status !== "two_minor_versions_behind" && version.status !== "two_or_more_minor_versions_behind") {
    return [];
  }
  return [
    {
      id: "version",
      tone: "warn",
      kind: "Version",
      title: "Kubernetes version is behind",
      detail: [
        `${version.current || "?"} → ${version.latest || "?"}`,
        version.minorVersionsBehind ? `${version.minorVersionsBehind} minor versions behind` : "",
      ]
        .filter(Boolean)
        .join(" · "),
      target: { page: "upgrade" },
    },
  ];
}

/** Ranked "Needs attention" rows for a dashboard summary. */
export function buildAttentionItems(summary) {
  if (!summary) return [];
  if (summary.health?.status === "unreachable") {
    return [
      {
        id: "cluster:unreachable",
        tone: "danger",
        kind: "Cluster",
        title: "Cluster is unreachable",
        detail: (summary.health.reasons || []).join(" · ") || "The API server did not answer",
        target: { page: "clusters" },
      },
    ];
  }
  const items = [
    ...nodeItems(summary.nodeHealth),
    ...podItems(summary),
    ...alertItems(summary),
    ...versionItem(summary),
  ];
  // Stable: keeps category order (nodes, pods, alerts, version) within a tone.
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => TONE_ORDER[a.item.tone] - TONE_ORDER[b.item.tone] || a.index - b.index)
    .map(({ item }) => item);
}
