import { useMemo } from "react";
import { formatDashboardTime } from "../utils/dashboardStatus.js";
import { buildAttentionItems, formatBytes, usageTone } from "./attention.js";

// KubeSight Operations Dashboard — triage first. Top to bottom it answers:
// is anything broken (Needs attention), how much room is left (vitals), which
// node is the problem (node table with CPU/memory/disk), and what changed
// lately. Everything shown is a real reading from the summary; a value that
// could not be measured renders as "—" with the reason, never as zero.

function toneOf(status) {
  const s = String(status || "").toLowerCase();
  if (s === "critical" || s === "unreachable" || s === "failed") return "danger";
  if (s === "warning") return "warn";
  if (s === "healthy") return "ok";
  return "muted";
}

function label(status) {
  const s = String(status || "unknown");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// "191" → "191 cores"; displays that already carry a unit pass through.
function withUnit(display, unit) {
  if (display == null || display === "") return display;
  return /^[\d.,]+$/.test(String(display)) ? `${display} ${unit}` : display;
}

// Audit timestamps span days, so a bare clock time misleads; say how long ago.
function ago(iso, fallback) {
  const t = iso ? new Date(iso).getTime() : NaN;
  if (!Number.isFinite(t)) return fallback || "";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days}d ago`;
}

function pct(value) {
  return value == null || !Number.isFinite(Number(value)) ? null : Math.round(Number(value));
}

// ── icons (stroke, currentColor) ───────────────────────────────────
function Ic({ children, size = 14 }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
      strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  );
}
const IcAlert = () => (
  <Ic><path d="m21.7 18-8-14a2 2 0 0 0-3.5 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z" /><path d="M12 9v4M12 17h.01" /></Ic>
);
const IcInfo = () => <Ic><circle cx="12" cy="12" r="9" /><path d="M12 8h.01M12 12v5" /></Ic>;
const IcCheck = () => <Ic><path d="M20 6 9 17l-5-5" /></Ic>;
const IcArrow = () => <Ic size={13}><path d="M5 12h14M13 6l6 6-6 6" /></Ic>;
const IcRefresh = () => (
  <Ic><path d="M21 12a9 9 0 1 1-2.64-6.36L21 8" /><path d="M21 3v5h-5" /></Ic>
);

const PAGE_LABEL = {
  clusters: "Clusters",
  resources: "Resources",
  alerts: "Alerts",
  upgrade: "Upgrade",
  namespaces: "Namespaces",
  auditLogs: "Audit log",
};

function GoButton({ target, canOpen, onNavigate, children }) {
  if (!target || !canOpen?.(target.page)) return null;
  return (
    <button type="button" className="btn-ghost db-go" onClick={() => onNavigate?.(target.page, target.options)}>
      {children || PAGE_LABEL[target.page] || "Open"}
      <IcArrow />
    </button>
  );
}

// ── Needs attention ────────────────────────────────────────────────
function Attention({ items, summary, canOpen, onNavigate }) {
  if (!items.length) {
    const nodes = summary?.nodes || {};
    const running = summary?.pods?.running ?? 0;
    return (
      <section className="db-card db-clear" aria-label="Needs attention">
        <span className="db-clear-ic"><IcCheck /></span>
        <div>
          <h2>Nothing needs attention</h2>
          <p>
            {nodes.ready}/{nodes.total} nodes ready · {running.toLocaleString()} pods running · no critical alerts
          </p>
        </div>
      </section>
    );
  }
  const worst = items[0].tone;
  return (
    <section className={`db-card db-attn db-attn--${worst}`} aria-label="Needs attention">
      <div className="db-card-h">
        <h2>Needs attention</h2>
        <span className={`db-count db-count--${worst}`}>{items.length}</span>
      </div>
      <ul className="db-attn-list">
        {items.map((item) => (
          <li key={item.id} className={`db-attn-row db-attn-row--${item.tone}`}>
            <span className="db-attn-ic">{item.tone === "info" ? <IcInfo /> : <IcAlert />}</span>
            <span className="db-attn-kind">{item.kind}</span>
            <div className="db-attn-body">
              <p className="db-attn-title" title={item.title}>{item.title}</p>
              {item.detail ? <p className="db-attn-detail">{item.detail}</p> : null}
            </div>
            <GoButton target={item.target} canOpen={canOpen} onNavigate={onNavigate} />
          </li>
        ))}
      </ul>
    </section>
  );
}

// ── vitals ─────────────────────────────────────────────────────────
function Meter({ name, percent, used, total, note }) {
  const value = pct(percent);
  const tone = usageTone(value);
  return (
    <div className="db-meter" title={note || undefined}>
      <span className="db-meter-name">{name}</span>
      <span className="db-bar" aria-hidden="true">
        {value != null ? <span className={`db-bar-fill db-bar-fill--${tone}`} style={{ width: `${Math.min(value, 100)}%` }} /> : null}
      </span>
      <span className={`db-meter-val${value != null ? ` db-meter-val--${tone}` : ""}`}>
        {value != null ? `${value}%` : "—"}
      </span>
      <span className="db-meter-sub">{value != null && used && total ? `${used} / ${total}` : note ? "not measured" : ""}</span>
    </div>
  );
}

function Vitals({ summary }) {
  const nodes = summary?.nodes || { ready: 0, total: 0 };
  const pods = summary?.pods || {};
  const alerts = summary?.alerts || {};
  const cpu = summary?.cpuUsage || {};
  const mem = summary?.memoryUsage || {};
  const disk = summary?.diskUsage || {};
  const cordoned = (summary?.nodeHealth || []).filter((n) => n.cordoned).length;
  const notReady = Math.max((nodes.total || 0) - (nodes.ready || 0), 0);
  const diskNote = !disk.available
    ? disk.reason
    : disk.measuredNodes < disk.totalNodes
      ? `Measured on ${disk.measuredNodes} of ${disk.totalNodes} nodes`
      : "";

  return (
    <div className="db-vitals">
      <div className="db-tile">
        <p className="db-tile-label">Nodes ready</p>
        <p className="db-tile-value">
          <b className={notReady ? "db-t--danger" : undefined}>{nodes.ready}</b>
          <span>/ {nodes.total}</span>
        </p>
        <p className="db-tile-sub">
          {notReady ? <span className="db-t--danger">{notReady} not ready</span> : "All ready"}
          {cordoned ? ` · ${cordoned} cordoned` : ""}
        </p>
      </div>
      <div className="db-tile">
        <p className="db-tile-label">Pods running</p>
        <p className="db-tile-value"><b>{(pods.running ?? 0).toLocaleString()}</b></p>
        <p className="db-tile-sub">
          <span className={pods.failed ? "db-t--danger" : undefined}>{pods.failed || 0} failing</span>
          {" · "}
          <span className={pods.pending ? "db-t--warn" : undefined}>{pods.pending || 0} pending</span>
        </p>
      </div>
      <div className="db-tile">
        <p className="db-tile-label">Active alerts</p>
        <p className="db-tile-value">
          <b className={alerts.critical ? "db-t--danger" : undefined}>{alerts.total ?? 0}</b>
        </p>
        <p className="db-tile-sub">
          {alerts.total ? (
            <>
              <span className={alerts.critical ? "db-t--danger" : undefined}>{alerts.critical || 0} critical</span>
              {" · "}
              <span className={alerts.warning ? "db-t--warn" : undefined}>{alerts.warning || 0} warning</span>
            </>
          ) : (
            "None active"
          )}
        </p>
      </div>
      <div className="db-tile db-tile--capacity">
        <p className="db-tile-label">Cluster capacity used</p>
        <Meter
          name="CPU"
          percent={cpu.available ? cpu.percent : null}
          used={cpu.usedDisplay}
          total={withUnit(cpu.allocatableDisplay, "cores")}
          note={cpu.available ? "" : cpu.reason}
        />
        <Meter
          name="Memory"
          percent={mem.available ? mem.percent : null}
          used={mem.usedDisplay}
          total={mem.allocatableDisplay}
          note={mem.available ? "" : mem.reason}
        />
        <Meter
          name="Disk"
          percent={disk.available ? disk.percent : null}
          used={disk.available ? formatBytes(disk.usedBytes) : ""}
          total={disk.available ? formatBytes(disk.totalBytes) : ""}
          note={diskNote}
        />
      </div>
    </div>
  );
}

// ── node table ─────────────────────────────────────────────────────
function UsageCell({ name, percent, detail, missing }) {
  const value = pct(percent);
  const tone = usageTone(value);
  return (
    <td className="db-use" data-label={name}>
      <div className="db-use-top">
        <span className="db-bar" aria-hidden="true">
          {value != null ? <span className={`db-bar-fill db-bar-fill--${tone}`} style={{ width: `${Math.min(value, 100)}%` }} /> : null}
        </span>
        <b className={value != null ? `db-use-pct db-use-pct--${tone}` : "db-use-pct"}>{value != null ? `${value}%` : "—"}</b>
      </div>
      <span className="db-use-sub">{value != null ? detail : missing}</span>
    </td>
  );
}

function NodeTable({ nodes, disk, canOpen, onNavigate }) {
  const diskNote = !disk?.available && disk?.reason ? disk.reason : "";
  return (
    <section className="db-card db-nodes" aria-label="Nodes">
      <div className="db-card-h">
        <h2>Nodes</h2>
        <span className="db-card-sub">{nodes.length} · worst first</span>
        <div className="db-card-r">
          {diskNote ? (
            <span className="db-note" title={diskNote}>
              <IcInfo />
              Disk usage not readable
              {/nodes\/proxy|forbidden/i.test(diskNote) ? <> · needs <code>nodes/proxy</code></> : null}
            </span>
          ) : null}
          <GoButton target={{ page: "clusters" }} canOpen={canOpen} onNavigate={onNavigate} />
        </div>
      </div>
      {nodes.length ? (
        <div className="db-table-wrap">
          <table className="db-table">
            <thead>
              <tr>
                <th scope="col">Node</th>
                <th scope="col">Status</th>
                <th scope="col">CPU</th>
                <th scope="col">Memory</th>
                <th scope="col" title="The filesystem containerd keeps images and container layers on (kubelet runtime imageFs); the root filesystem when the kubelet does not report one">Containerd disk</th>
                <th scope="col" className="db-num">Pods</th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((node) => {
                const tone = toneOf(node.status);
                const flags = [...(node.pressures || [])];
                if (!node.ready) flags.unshift("NotReady");
                return (
                  <tr key={node.name} className={`db-row db-row--${tone}`}>
                    <th scope="row" className="db-node">
                      <span className="db-node-name" title={node.name}>{node.name}</span>
                      <span className="db-node-meta">
                        {(node.roles || []).join(", ")}
                        {node.kubeletVersion ? ` · ${node.kubeletVersion}` : ""}
                        {node.cordoned ? <span className="db-tag">cordoned</span> : null}
                      </span>
                    </th>
                    <td className="db-node-status">
                      <span className={`db-status db-status--${tone}`}>
                        <span className="db-dot" />
                        {label(node.status)}
                      </span>
                      {flags.length ? (
                        <span className="db-flags">
                          {flags.map((flag) => (
                            <span key={flag} className="db-flag">{flag}</span>
                          ))}
                        </span>
                      ) : null}
                    </td>
                    <UsageCell
                      name="CPU"
                      percent={node.cpuPercent}
                      detail={`${node.cpuUsedCores ?? "—"} / ${node.cpuTotalCores ?? "—"} cores`}
                      missing={node.cpuTotalCores ? `of ${node.cpuTotalCores} cores` : ""}
                    />
                    <UsageCell
                      name="Memory"
                      percent={node.memoryPercent}
                      detail={`${formatBytes((node.memoryUsedMiB || 0) * 1024 * 1024)} / ${formatBytes((node.memoryTotalMiB || 0) * 1024 * 1024)}`}
                      missing={node.memoryTotalMiB ? `of ${formatBytes(node.memoryTotalMiB * 1024 * 1024)}` : ""}
                    />
                    <UsageCell
                      name="Disk"
                      percent={node.diskPercent}
                      detail={`${formatBytes(node.diskUsedBytes)} / ${formatBytes(node.diskTotalBytes)}${node.diskSource === "node" ? " · root fs" : ""}`}
                      missing="not measured"
                    />
                    <td className="db-num db-pods" data-label="Pods">
                      {node.podsRunning ?? "—"}
                      {node.podsCapacity ? <span> / {node.podsCapacity}</span> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="db-empty">No node data for this cluster.</p>
      )}
    </section>
  );
}

// ── namespaces + activity ──────────────────────────────────────────
function Namespaces({ namespaces, canOpen, onNavigate }) {
  const flagged = namespaces.filter((ns) => ns.status !== "healthy");
  const rows = flagged.length ? flagged : namespaces.slice(0, 5);
  return (
    <section className="db-card" aria-label="Namespaces">
      <div className="db-card-h">
        <h2>{flagged.length ? "Namespaces with alerts" : "Busiest namespaces"}</h2>
        <div className="db-card-r">
          <GoButton target={{ page: "namespaces" }} canOpen={canOpen} onNavigate={onNavigate}>View all</GoButton>
        </div>
      </div>
      {rows.length ? (
        <ul className="db-list">
          {rows.map((ns) => {
            const tone = toneOf(ns.status);
            return (
              <li key={ns.name} className="db-list-row">
                <span className={`db-dot db-dot--${tone}`} />
                <span className="db-list-name" title={ns.name}>{ns.name}</span>
                <span className="db-list-meta">{ns.pods} pods</span>
                {ns.alertCount ? (
                  <span className={`db-pill db-pill--${tone}`}>{ns.alertCount} alert{ns.alertCount === 1 ? "" : "s"}</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="db-empty">No namespaces you can see on this cluster.</p>
      )}
    </section>
  );
}

function eventTone(event) {
  const text = `${event.action || ""} ${event.message || ""}`;
  if (/fail|error|critical|denied/i.test(text)) return "danger";
  if (/warn|rollback/i.test(text)) return "warn";
  return "muted";
}

function Activity({ events, canOpen, onNavigate }) {
  return (
    <section className="db-card" aria-label="Recent activity">
      <div className="db-card-h">
        <h2>Recent activity</h2>
        <div className="db-card-r">
          <GoButton target={{ page: "auditLogs" }} canOpen={canOpen} onNavigate={onNavigate}>View all</GoButton>
        </div>
      </div>
      {events.length ? (
        <ul className="db-list">
          {events.map((event, i) => (
            <li key={`${event.createdAt || event.time}-${i}`} className="db-list-row db-event">
              <span className={`db-dot db-dot--${eventTone(event)}`} />
              <span className="db-event-msg">{event.message}</span>
              <span className="db-list-meta" title={event.createdAt || undefined}>{ago(event.createdAt, event.time)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="db-empty">No recent activity recorded.</p>
      )}
    </section>
  );
}

export default function OpsDashboard({
  summary,
  isAdmin = true,
  lastRefreshedAt,
  refreshing = false,
  onRefresh,
  onNavigate,
  canOpen,
}) {
  const health = summary?.health?.status || summary?.clusterHealth?.status || "unknown";
  const clusterInfo = summary?.clusterInfo || {};
  const version = summary?.version || {};
  const nodes = summary?.nodeHealth || [];
  const items = useMemo(() => buildAttentionItems(summary), [summary]);
  const events = useMemo(
    () => [...(summary?.operationalEvents || []), ...(summary?.recentActivity || [])].slice(0, 6),
    [summary?.operationalEvents, summary?.recentActivity]
  );

  return (
    <div className="db-root">
      <header className="sg-ph db-head">
        <div>
          <div className="db-title">
            <h1>{isAdmin ? "Operations Dashboard" : "Dashboard"}</h1>
            <span className={`db-health db-health--${toneOf(health)}`}>
              <span className="db-dot" />
              {label(health)}
            </span>
          </div>
          <p className="sg-ph-sub">
            {clusterInfo.name || summary?.clusterId}
            {version.current ? <> · <span className="db-mono">{version.current}</span></> : null}
            {clusterInfo.provider && clusterInfo.provider !== "Unknown" ? ` · ${clusterInfo.provider}` : ""}
            {" · updated "}
            <span className="db-mono">{formatDashboardTime(lastRefreshedAt || summary?.lastUpdated)}</span>
          </p>
        </div>
        <div className="sg-ph-actions">
          <button
            type="button"
            className={`btn-ghost db-refresh${refreshing ? " is-busy" : ""}`}
            onClick={onRefresh}
            disabled={refreshing}
          >
            <IcRefresh />
            {refreshing ? "Refreshing" : "Refresh"}
          </button>
        </div>
      </header>

      <Attention items={items} summary={summary} canOpen={canOpen} onNavigate={onNavigate} />
      <Vitals summary={summary} />
      <NodeTable nodes={nodes} disk={summary?.diskUsage} canOpen={canOpen} onNavigate={onNavigate} />
      <div className="db-row-2">
        <Namespaces namespaces={summary?.namespaces || []} canOpen={canOpen} onNavigate={onNavigate} />
        <Activity events={events} canOpen={canOpen} onNavigate={onNavigate} />
      </div>
    </div>
  );
}
