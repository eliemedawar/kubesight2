export function resolveDisplayUser(authUser) {
  if (!authUser) {
    return { name: "User", role: "" };
  }
  const name =
    (authUser.fullName || authUser.username || authUser.email || "User").trim() || "User";
  return {
    name,
    role: authUser.role || "user",
  };
}

export function getUserInitials(name) {
  const parts = String(name || "User")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) {
    return "U";
  }
  return parts
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export const emptyAppData = {
  clusters: [],
  namespaces: [],
  resources: {
    pods: [],
    deployments: [],
    replicasets: [],
    statefulsets: [],
    daemonsets: [],
    jobs: [],
    cronjobs: [],
    services: [],
  },
  alerts: [],
  alertsMeta: {},
  notificationChannels: [],
  settings: {},
};

export const defaultAlertRouting = () => ({
  email: { enabled: false, address: "" },
  slack: { enabled: false, webhookUrl: "" },
  webhook: { enabled: false, url: "" },
});

export const normalizeAlertRouting = (routing) => {
  const normalized = defaultAlertRouting();
  if (!routing || typeof routing !== "object") {
    return normalized;
  }
  Object.keys(normalized).forEach((channel) => {
    const incoming = routing[channel];
    if (!incoming || typeof incoming !== "object") {
      return;
    }
    normalized[channel] = {
      ...normalized[channel],
      ...incoming,
      enabled: Boolean(incoming.enabled),
    };
    if (channel === "email") {
      normalized[channel].address = String(incoming.address || "").trim();
    }
    if (channel === "slack") {
      normalized[channel].webhookUrl = String(incoming.webhookUrl || "").trim();
    }
    if (channel === "webhook") {
      normalized[channel].url = String(incoming.url || "").trim();
    }
  });
  return normalized;
};

const routingChannelState = (enabled, configured) => {
  if (!enabled) {
    return "Disabled";
  }
  return configured ? "Configured" : "Needs setup";
};

export const buildNotificationChannels = (settings = {}) => {
  const routing = normalizeAlertRouting(settings.notifications?.routing);
  const rows = [
    {
      channel:
        routing.email.enabled && routing.email.address
          ? `Email · ${routing.email.address}`
          : "Email",
      type: "Email",
      state: routingChannelState(routing.email.enabled, routing.email.address.includes("@")),
    },
    {
      channel: routing.slack.enabled ? "Slack · Incoming webhook" : "Slack",
      type: "Slack",
      state: routingChannelState(
        routing.slack.enabled,
        routing.slack.webhookUrl.startsWith("https://hooks.slack.com/")
      ),
    },
    {
      channel:
        routing.webhook.enabled && routing.webhook.url
          ? `Webhook · ${routing.webhook.url}`
          : "Webhook",
      type: "HTTP",
      state: routingChannelState(
        routing.webhook.enabled,
        routing.webhook.url.startsWith("http://") || routing.webhook.url.startsWith("https://")
      ),
    },
    {
      channel: "Upgrade events",
      type: "System",
      state: settings.notifications?.upgrades ? "Enabled" : "Disabled",
    },
  ];

  if (!settings.notifications?.alerts) {
    return rows.map((row) =>
      row.channel === "Upgrade events"
        ? row
        : { ...row, state: row.state === "Disabled" ? "Disabled" : "Paused" }
    );
  }
  return rows;
};

export const resolveDefaultClusterId = (clusters, preferredId) => {
  if (!clusters.length) {
    return "";
  }
  if (preferredId && clusters.some((cluster) => cluster.id === preferredId)) {
    return preferredId;
  }
  return clusters[0]?.id || "";
};

export const normalizeSettings = (settings = {}) => ({
  theme: ["system", "dark", "light"].includes(settings.theme) ? settings.theme : "system",
  refreshIntervalSeconds:
    Number.isFinite(Number(settings.refreshIntervalSeconds)) &&
    Number(settings.refreshIntervalSeconds) > 0
      ? Number(settings.refreshIntervalSeconds)
      : 30,
  defaultCluster: settings.defaultCluster || "",
  notifications: {
    alerts: Boolean(settings.notifications?.alerts ?? true),
    upgrades: Boolean(settings.notifications?.upgrades ?? true),
    routing: normalizeAlertRouting(settings.notifications?.routing),
  },
});

export const mapPrecheckState = (status) => {
  if (status === "passed") {
    return "PASS";
  }
  if (status === "warning") {
    return "WARNING";
  }
  if (status === "failed") {
    return "FAIL";
  }
  return "Pending";
};

export const mapPrecheckClass = (status) => {
  if (status === "passed" || status === "PASS") {
    return "pass";
  }
  if (status === "warning" || status === "WARNING") {
    return "warning";
  }
  if (status === "failed" || status === "FAIL") {
    return "fail";
  }
  return "pending";
};
