import {
  IconArrowRight,
  IconGrid,
  IconInbox,
  IconRefresh,
  IconServer,
  IconZap,
} from "./icons.jsx";
import { ACTIVE_RUN_STATUSES } from "./ZohoRunDetail.jsx";
import { timeAgo } from "./common.jsx";

// Tone ranking for connectors: a link inherits the worse of its two nodes.
const TONE_RANK = { ok: 0, info: 1, muted: 2, warn: 3, danger: 4 };
const worseTone = (a, b) => (TONE_RANK[a] >= TONE_RANK[b] ? a : b);

function Node({ icon: Icon, title, metric, sub, tone, pill, onClick }) {
  return (
    <button
      type="button"
      className={`sg-zh-fnode ${tone === "warn" || tone === "danger" ? `sg-zh-fnode--${tone}` : ""}`}
      onClick={onClick}
    >
      <span className="sg-zh-fnode-head">
        <Icon />
        <b>{title}</b>
      </span>
      <span className="sg-zh-fnode-metric">{metric}</span>
      <span className="sg-zh-fnode-sub">{sub}</span>
      <span className={`status-pill ${tone}`}>
        <span className="sg-zh-fnode-dot" />
        {pill}
      </span>
    </button>
  );
}

// The integration drawn as the pipeline it is:
// cluster → field sync → Zoho Desk → inbound tickets → deploy automation.
// Each node carries its own health; clicking a node opens the room that manages it.
export default function ZohoFlowStrip({
  config,
  preview,
  previewLoading,
  tickets,
  ticketsLoading,
  runs,
  jenkins,
  onNavigate,
}) {
  const enabled = Boolean(config?.enabled);
  const namespaces = config?.selectedNamespaces || [];

  // Source cluster (custom Jenkins-only environments count as a source too)
  const hasSource = Boolean(
    config?.sourceClusterId || (config?.customEnvironments || []).length
  );
  const sourceTone = hasSource ? "ok" : "warn";

  // Field sync
  const syncTone = !enabled
    ? "muted"
    : config?.lastSyncStatus === "ok"
    ? "ok"
    : config?.lastSyncStatus
    ? "danger"
    : "muted";
  const syncPill = !enabled
    ? "Disabled"
    : config?.lastSyncStatus === "ok"
    ? "OK"
    : config?.lastSyncStatus
    ? "Failed"
    : "Never ran";

  // Zoho Desk (published dropdowns)
  const previewCount = preview?.count ?? 0;
  const namespaceCount = (preview?.namespaces || []).length;
  const zohoTone =
    config?.lastTestStatus === "ok" ? "ok" : config?.lastTestStatus ? "danger" : "muted";
  const zohoPill =
    config?.lastTestStatus === "ok"
      ? "Connected"
      : config?.lastTestStatus
      ? "Unreachable"
      : "Untested";

  // Inbound tickets
  const unresolved = tickets.filter((t) => !t.resolved).length;
  const secretSet = Boolean(config?.inboundSecretConfigured);
  const inboundTone = unresolved > 0 ? "danger" : secretSet ? "ok" : "warn";
  const inboundPill =
    unresolved > 0 ? `${unresolved} unresolved` : secretSet ? "OK" : "No secret";

  // Deploy automation
  const activeRuns = runs.filter((r) => ACTIVE_RUN_STATUSES.has(r.status));
  const awaiting = runs.filter((r) => r.status === "awaiting_approval").length;
  const jenkinsOn = Boolean(jenkins?.enabled);
  const autoTone = !jenkinsOn ? "muted" : awaiting > 0 ? "warn" : activeRuns.length > 0 ? "info" : "ok";
  const autoPill = !jenkinsOn
    ? "Off"
    : awaiting > 0
    ? `${awaiting} awaiting approval`
    : activeRuns.length > 0
    ? `${activeRuns.length} running`
    : "Idle";

  const nodes = [
    {
      key: "source",
      icon: IconServer,
      title: "Source cluster",
      metric: hasSource ? config.sourceClusterId : "Not set",
      sub: hasSource ? `${namespaces.length} namespace(s) selected` : "pick one in Field sync",
      tone: sourceTone,
      pill: hasSource ? "Connected" : "Not set",
      target: "fieldsync",
    },
    {
      key: "sync",
      icon: IconRefresh,
      title: "Field sync",
      metric: config?.lastSyncAt ? timeAgo(config.lastSyncAt) : "Never",
      sub: `every ${config?.syncIntervalMinutes || 30} min · cascade ${
        config?.cascadeEnabled === false ? "off" : "on"
      }`,
      tone: syncTone,
      pill: syncPill,
      target: "overview",
    },
    {
      key: "zoho",
      icon: IconGrid,
      title: "Zoho Desk",
      metric: previewLoading ? "…" : `${previewCount} apps · ${namespaceCount} envs`,
      sub: "DevOps Request layout",
      tone: zohoTone,
      pill: zohoPill,
      target: "fieldsync",
    },
    {
      key: "inbound",
      icon: IconInbox,
      title: "Inbound tickets",
      metric: ticketsLoading ? "…" : `${tickets.length} received`,
      sub: secretSet ? "webhook secret set" : "webhook open — no secret",
      tone: inboundTone,
      pill: inboundPill,
      target: "tickets",
    },
    {
      key: "auto",
      icon: IconZap,
      title: "Deploy automation",
      metric: activeRuns.length > 0 ? `${activeRuns.length} active` : `${runs.length} run(s)`,
      sub: jenkinsOn ? "Jenkins router" : "Jenkins not connected",
      tone: autoTone,
      pill: autoPill,
      target: jenkinsOn ? "tickets" : "settings",
    },
  ];

  return (
    <div className="sg-zh-flow" role="group" aria-label="Integration pipeline">
      {nodes.map((node, index) => (
        <span key={node.key} className="sg-zh-fseg">
          {index > 0 ? (
            <span
              className={`sg-zh-flink sg-zh-flink--${worseTone(nodes[index - 1].tone, node.tone)}`}
              aria-hidden="true"
            >
              <IconArrowRight />
            </span>
          ) : null}
          <Node {...node} onClick={() => onNavigate(node.target)} />
        </span>
      ))}
    </div>
  );
}
