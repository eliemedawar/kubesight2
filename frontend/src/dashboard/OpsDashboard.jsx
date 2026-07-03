import { useMemo } from "react";
import ChartCanvas from "./charts/ChartCanvas.jsx";
import Sparkline from "./charts/Sparkline.jsx";
import { cssVar, drawArea, drawLines, drawStacked } from "./charts/chartDraw.js";
import { TIME_RANGES } from "./useDashboardSeries.js";
import { formatDashboardTime, formatLatestVersion } from "../utils/dashboardStatus.js";

// ── status helpers ─────────────────────────────────────────────────
// Tone name for pills / dots / bars, derived from the status word so all
// colors stay token-driven (status-pill + ov-dot/sg-bar-fill modifiers).
function pillTone(status) {
  const s = String(status || "").toLowerCase();
  if (s === "critical" || s === "failed" || s === "fail") return "danger";
  if (s === "warning" || s === "warn") return "warn";
  if (s === "healthy" || s === "passed" || s === "pass" || s === "ready") return "ok";
  return "unknown";
}

// Dot/bar tone: same mapping but "muted" for unknown (dots need a color).
function dotTone(status) {
  const tone = pillTone(status);
  return tone === "unknown" ? "muted" : tone;
}

function statusLabel(status) {
  const s = String(status || "unknown").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Format a KB/s throughput figure the way the reference does.
function fmtThroughput(value) {
  const v = Number(value) || 0;
  if (v >= 1000) return `${(v / 1000).toFixed(1)} MB/s`;
  return `${Math.round(v)} KB/s`;
}

// Format a memory figure given in MiB as GiB (e.g. 31744 -> "31.0 GiB").
function formatGiB(mib) {
  if (mib == null || Number.isNaN(Number(mib))) return "—";
  return `${(Number(mib) / 1024).toFixed(1)} GiB`;
}

// Simple trend marker from the rolling series (recent vs. a few samples back).
function trend(arr) {
  if (!arr || arr.length < 6) return null;
  const last = arr[arr.length - 1];
  const prev = arr[arr.length - 6];
  const delta = Math.round(Math.abs(last - prev));
  if (!delta) return null;
  return { dir: last >= prev ? "up" : "down", delta };
}

// Presentation tone for a feed entry, matched on its message/action text.
function eventTone(event) {
  const text = `${event.action || ""} ${event.message || ""}`;
  if (/fail|error|critical/i.test(text)) return "danger";
  if (/warn/i.test(text)) return "warn";
  if (/success|passed|completed|resolved|healthy/i.test(text)) return "ok";
  return "muted";
}

// ── inline icons (stroke-based, Signal style) ──────────────────────
function Ic({ children, size = 14, strokeWidth = 1.8 }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const IcServer = () => (
  <Ic>
    <rect x="3" y="4" width="18" height="7" rx="2.5" />
    <rect x="3" y="13" width="18" height="7" rx="2.5" />
    <path d="M7 7.5h.01M7 16.5h.01" />
  </Ic>
);

const IcCpu = () => (
  <Ic>
    <rect x="4" y="4" width="16" height="16" rx="2" />
    <rect x="9" y="9" width="6" height="6" />
    <path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
  </Ic>
);

const IcMemory = () => (
  <Ic>
    <rect x="3" y="5" width="18" height="11" rx="2" />
    <path d="M7 19v-3M12 19v-3M17 19v-3" />
  </Ic>
);

const IcNodes = () => (
  <Ic>
    <circle cx="6" cy="12" r="2.6" />
    <circle cx="18" cy="5" r="2.6" />
    <circle cx="18" cy="19" r="2.6" />
    <path d="m8.4 10.7 7.2-4.5M8.4 13.3l7.2 4.5" />
  </Ic>
);

const IcBox = () => (
  <Ic>
    <path d="M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8Z" />
    <path d="M3.3 7 12 12l8.7-5M12 12v10" />
  </Ic>
);

const IcAlert = () => (
  <Ic>
    <path d="m21.7 18-8-14a2 2 0 0 0-3.5 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z" />
    <path d="M12 9v4M12 17h.01" />
  </Ic>
);

const IcRefresh = () => (
  <Ic>
    <path d="M21 12a9 9 0 1 1-2.64-6.36L21 8" />
    <path d="M21 3v5h-5" />
  </Ic>
);

const IcRocket = () => (
  <Ic>
    <path d="M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2.18 2.18 0 0 0-2.9-.1Z" />
    <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2Z" />
    <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
  </Ic>
);

const IcArrow = () => (
  <Ic strokeWidth={2}>
    <path d="M7 17 17 7M8 7h9v9" />
  </Ic>
);

const IcCheck = () => (
  <Ic strokeWidth={2.2}>
    <path d="M20 6 9 17l-5-5" />
  </Ic>
);

const IcInfo = () => (
  <Ic>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 8h.01M12 12v5" />
  </Ic>
);

// ── small building blocks ──────────────────────────────────────────
function KvRow({ label, value, mono, tone }) {
  const valClass = [
    "ov-kv-val",
    mono ? "ov-kv-val--mono" : "",
    tone ? `ov-kv-val--${tone}` : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="ov-kv-row">
      <span className="ov-kv-key">{label}</span>
      <span className={valClass}>{value}</span>
    </div>
  );
}

function DeltaChip({ tone = "flat", children }) {
  return <span className={`sg-delta sg-delta--${tone}`}>{children}</span>;
}

// KubeSight Operations Dashboard, restructured to the Signal concept's
// Overview screen: sg-ph page header, sg-kpi tiles with sparklines, Signal
// card headers on the chart panels, an sg-flist namespace card and an
// sg-feed events card. Fed by the real dashboard summary + the rolling
// series hook; the app's AppShell provides the surrounding chrome.
export default function OpsDashboard({
  summary,
  series,
  timeRange,
  onTimeRangeChange,
  lastRefreshedAt,
  onRefresh,
  canOpenUpgrade,
  onNavigateToUpgrade,
  onViewAllEvents,
}) {
  const health = summary?.health?.status || summary?.clusterHealth?.status || "healthy";
  const healthReasons = summary?.health?.reasons || summary?.clusterHealth?.reasons || [];
  const cpu = summary?.cpuUsage || {};
  const mem = summary?.memoryUsage || {};
  const nodes = summary?.nodes || { ready: 0, total: 0, status: "unknown" };
  const pods = summary?.pods || { running: 0, pending: 0, failed: 0 };
  const alerts = summary?.alerts || { critical: 0, warning: 0, info: 0, total: 0 };
  const version = summary?.version || {};
  const clusterInfo = summary?.clusterInfo || {};
  const namespaces = summary?.namespaces || [];
  const nodeHealth = summary?.nodeHealth || [];
  const events = useMemo(
    () => [...(summary?.operationalEvents || []), ...(summary?.recentActivity || [])].slice(0, 10),
    [summary?.operationalEvents, summary?.recentActivity]
  );

  // Canvas charts need resolved colors; these are read from design tokens at
  // render time (theme-aware) — never hardcoded.
  const accent = cssVar("--accent", "#3b82f6");
  const TEAL = cssVar("--chart-8", "#2dd4bf");
  const PURPLE = cssVar("--chart-3", "#8b5cf6");
  const bands = series?.cpuBands || [];
  const bandColors = bands.map((_, i) => (i === 0 ? accent : i === 1 ? TEAL : PURPLE));
  // Same palette as var() names for DOM legend dots (no resolved hex in JSX).
  const bandTokens = ["--accent", "--chart-8", "--chart-3"];
  const cpuPeak = series?.cpu?.length ? Math.round(Math.max(...series.cpu)) : null;
  const netIn = series?.netIn || [];
  const netOut = series?.netOut || [];

  const cpuTrend = trend(series?.cpu);
  const memTrend = trend(series?.mem);
  const healthTone = pillTone(health);
  const notReady = Math.max((nodes.total ?? 0) - (nodes.ready ?? 0), 0);

  const versionUpToDate = version.status === "up_to_date";

  const dateLine = new Date().toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  });

  return (
    <div className="ov-dash">
      {/* ── Page header ───────────────────────────────────────── */}
      <header className="sg-ph">
        <div>
          <div className="ov-title-line">
            <h1>Operations Dashboard</h1>
            <span className={`status-pill ${healthTone}`}>{statusLabel(health)}</span>
          </div>
          <p className="sg-ph-sub">
            {dateLine} · {clusterInfo.name || summary?.clusterId || "cluster"} ·{" "}
            <span className="ov-mono">{version.current || clusterInfo.version || "—"}</span> · updated{" "}
            <span className="ov-mono">{formatDashboardTime(lastRefreshedAt || summary?.lastUpdated)}</span>
          </p>
        </div>
        <div className="sg-ph-actions">
          <div className="ov-range" role="group" aria-label="Chart time range">
            {TIME_RANGES.map((range) => (
              <button
                key={range}
                type="button"
                className={`ov-range-pill${timeRange === range ? " is-active" : ""}`}
                aria-pressed={timeRange === range}
                onClick={() => onTimeRangeChange?.(range)}
              >
                {range}
              </button>
            ))}
          </div>
          <button type="button" className="btn-ghost ov-ghost" onClick={onRefresh}>
            <IcRefresh />
            Refresh
          </button>
          {canOpenUpgrade ? (
            <button type="button" className="primary" onClick={onNavigateToUpgrade}>
              <IcRocket />
              Upgrade Safe Mode
            </button>
          ) : null}
        </div>
      </header>

      {/* ── KPI tiles ─────────────────────────────────────────── */}
      <div className="sg-kpi-grid">
        <div className="sg-kpi">
          <p className="sg-kpi-label">
            <IcServer />
            Cluster health
          </p>
          <div className="sg-kpi-value">
            <b className={`ov-kpi-word ov-kpi-word--${dotTone(health)}`}>{statusLabel(health)}</b>
          </div>
          <div className="ov-kpi-sub">
            {nodes.ready}/{nodes.total} nodes · {clusterInfo.podCount ?? pods.running} pods
          </div>
          {health !== "healthy" && healthReasons.length ? (
            <ul className={`ov-kpi-reasons ov-kpi-reasons--${dotTone(health)}`}>
              {healthReasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
        </div>

        <div className="sg-kpi">
          <p className="sg-kpi-label">
            <IcCpu />
            CPU usage
          </p>
          <div className="sg-kpi-value">
            <b>{cpu.available ? `${cpu.percent}%` : "—"}</b>
            {cpuTrend ? (
              <DeltaChip tone="flat">
                {cpuTrend.dir === "up" ? "▲" : "▼"} {cpuTrend.delta}%
              </DeltaChip>
            ) : null}
          </div>
          <div className="sg-spark">
            <Sparkline data={series?.cpu} color="--chart-2" height={34} />
          </div>
          <div className="ov-kpi-sub">of {cpu.allocatableDisplay || "cluster vCPU"}</div>
        </div>

        <div className="sg-kpi">
          <p className="sg-kpi-label">
            <IcMemory />
            Memory usage
          </p>
          <div className="sg-kpi-value">
            <b>{mem.available ? `${mem.percent}%` : "—"}</b>
            {memTrend ? (
              <DeltaChip tone="flat">
                {memTrend.dir === "up" ? "▲" : "▼"} {memTrend.delta}%
              </DeltaChip>
            ) : null}
          </div>
          <div className="sg-spark">
            <Sparkline data={series?.mem} color="--chart-4" height={34} />
          </div>
          <div className="ov-kpi-sub">of {mem.allocatableDisplay || "allocatable"}</div>
        </div>

        <div className="sg-kpi">
          <p className="sg-kpi-label">
            <IcNodes />
            Nodes
          </p>
          <div className="sg-kpi-value">
            <b>
              {nodes.ready} / {nodes.total}
            </b>
            {notReady > 0 ? <DeltaChip tone="down">{notReady} not ready</DeltaChip> : null}
          </div>
          <div className="ov-kpi-sub">
            {nodes.ready === nodes.total && nodes.total > 0 ? "All ready" : statusLabel(nodes.status)}
          </div>
        </div>

        <div className="sg-kpi">
          <p className="sg-kpi-label">
            <IcBox />
            Running pods
          </p>
          <div className="sg-kpi-value">
            <b>{pods.running}</b>
            {pods.failed > 0 ? (
              <DeltaChip tone="down">{pods.failed} failed</DeltaChip>
            ) : pods.pending > 0 ? (
              <DeltaChip tone="flat">{pods.pending} pending</DeltaChip>
            ) : null}
          </div>
          <div className="ov-kpi-sub">
            Pending {pods.pending} · Failed {pods.failed}
          </div>
        </div>

        <div className="sg-kpi">
          <p className="sg-kpi-label">
            <IcAlert />
            Active alerts
          </p>
          <div className="sg-kpi-value">
            <b>{alerts.total}</b>
            {alerts.critical > 0 ? (
              <DeltaChip tone="down">{alerts.critical} critical</DeltaChip>
            ) : alerts.warning > 0 ? (
              <span className="sg-delta ov-delta--warn">{alerts.warning} warning</span>
            ) : null}
          </div>
          <div className="ov-kpi-sub">
            Critical {alerts.critical} · Warning {alerts.warning}
          </div>
        </div>
      </div>

      {/* ── CPU chart + namespace fleet list ──────────────────── */}
      <div className="sg-row-2">
        <section className="ov-card">
          <div className="ov-card-h">
            <h3>CPU Utilization</h3>
            <span className="ov-card-sub">
              By namespace · % of cluster
              {cpuPeak != null ? ` · peak ${cpuPeak}%` : ""}
            </span>
            <div className="ov-card-r">
              <div className="ov-legend">
                {bands.map((band, i) => (
                  <i key={band.label}>
                    <span
                      className="ov-sq"
                      style={{ background: `var(${bandTokens[i] || "--chart-5"})` }}
                    />
                    {band.label}
                  </i>
                ))}
              </div>
              {!series?.cpuReal ? <span className="ov-sample">sample split</span> : null}
            </div>
          </div>
          <div className="ov-chart-wrap">
            <ChartCanvas
              className="ov-chart ov-chart--tall"
              draw={(ctx, { width, height }) => {
                if (!bands.length) return;
                drawStacked(ctx, width, height, bands.map((b) => b.data), bandColors, 100, "%");
              }}
              deps={[bands, accent]}
            />
          </div>
        </section>

        <section className="ov-card">
          <div className="ov-card-h">
            <h3>Namespaces</h3>
            <span className="ov-card-sub">{namespaces.length} total</span>
          </div>
          {namespaces.length ? (
            <div className="sg-flist ov-scroll">
              {namespaces.map((ns) => (
                <div className="sg-fl" key={ns.name}>
                  <span className={`sg-fl-dot ov-dot--${dotTone(ns.status)}`} />
                  <span className="sg-fl-name ov-mono" title={ns.name}>
                    {ns.name}
                  </span>
                  <span className="sg-fl-meta">{ns.pods} pods</span>
                  <span className={`status-pill status-pill--compact ${pillTone(ns.status)}`}>
                    {statusLabel(ns.status)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="ov-empty">No namespaces available for this cluster.</div>
          )}
        </section>
      </div>

      {/* ── Memory + network charts ───────────────────────────── */}
      <div className="sg-row-11">
        <section className="ov-card">
          <div className="ov-card-h">
            <h3>Memory Utilization</h3>
            <span className="ov-card-sub">
              Working set · % of allocatable
              {mem.usedDisplay && mem.allocatableDisplay
                ? ` · ${mem.usedDisplay} / ${mem.allocatableDisplay}`
                : ""}
            </span>
            <div className="ov-card-r">
              <div className="ov-legend">
                <i>
                  <span className="ov-sq" style={{ background: "var(--chart-3)" }} />
                  used
                </i>
                <i>
                  <span className="ov-dashline" />
                  limit {series?.memLimit || 85}%
                </i>
              </div>
            </div>
          </div>
          <div className="ov-chart-wrap">
            <ChartCanvas
              className="ov-chart"
              draw={(ctx, { width, height }) => {
                if (!series?.mem?.length) return;
                drawArea(ctx, width, height, series.mem, PURPLE, 100, series.memLimit || 85, "%");
              }}
              deps={[series?.mem, series?.memLimit]}
            />
          </div>
        </section>

        <section className="ov-card">
          <div className="ov-card-h">
            <h3>Network I/O</h3>
            <span className="ov-card-sub">Cluster-wide throughput</span>
            <div className="ov-card-r">
              <div className="ov-legend">
                <i>
                  <span className="ov-sq" style={{ background: "var(--accent)" }} />
                  Ingress <b className="ov-mono">{fmtThroughput(netIn[netIn.length - 1])}</b>
                </i>
                <i>
                  <span className="ov-sq" style={{ background: "var(--chart-8)" }} />
                  Egress <b className="ov-mono">{fmtThroughput(netOut[netOut.length - 1])}</b>
                </i>
              </div>
              {!series?.netReal ? <span className="ov-sample">sample</span> : null}
            </div>
          </div>
          <div className="ov-chart-wrap">
            <ChartCanvas
              className="ov-chart"
              draw={(ctx, { width, height }) => {
                if (!netIn.length || !netOut.length) return;
                drawLines(ctx, width, height, [netIn, netOut], [accent, TEAL], 1600, " KB");
              }}
              deps={[netIn, netOut, accent]}
            />
          </div>
        </section>
      </div>

      {/* ── Node health + events feed ─────────────────────────── */}
      <div className="sg-row-2">
        <section className="ov-card">
          <div className="ov-card-h">
            <h3>Node Health</h3>
            <span className="ov-card-sub">{nodeHealth.length} nodes</span>
          </div>
          {nodeHealth.length ? (
            <>
              <div className="ov-nt-head">
                <span>Node</span>
                <span>Status</span>
                <span>Memory (used / total)</span>
                <span className="ov-nt-sm">Free</span>
                <span className="ov-nt-sm">Usage</span>
              </div>
              {nodeHealth.map((node) => {
                const tone = dotTone(node.status);
                return (
                  <div className="ov-nt-row" key={node.name}>
                    <span className="ov-nt-name" title={node.name}>
                      {node.name}
                    </span>
                    <span className={`ov-status ov-status--${tone}`}>
                      <span className={`sg-fl-dot ov-dot--${tone}`} />
                      {statusLabel(node.status)}
                    </span>
                    <span className="ov-nt-mono">
                      {formatGiB(node.memoryUsedMiB)} / {formatGiB(node.memoryTotalMiB)}
                      {node.memoryPercent != null ? ` · ${node.memoryPercent}%` : ""}
                    </span>
                    <span className="ov-nt-mono ov-nt-sm">{formatGiB(node.memoryAvailableMiB)}</span>
                    <span className="sg-bar-track ov-nt-sm">
                      <span
                        className={`sg-bar-fill${tone !== "muted" ? ` sg-bar-fill--${tone}` : ""}`}
                        style={{ width: `${Math.min(node.memoryPercent ?? 0, 100)}%` }}
                      />
                    </span>
                  </div>
                );
              })}
            </>
          ) : (
            <div className="ov-empty">No node metrics available for this cluster.</div>
          )}
        </section>

        <section className="ov-card">
          <div className="ov-card-h">
            <h3>Events &amp; Alerts</h3>
            <div className="ov-card-r">
              <button type="button" className="ov-lnk" onClick={onViewAllEvents}>
                View all
                <IcArrow />
              </button>
            </div>
          </div>
          {events.length ? (
            <div className="sg-feed ov-scroll">
              {events.map((event, i) => {
                const tone = eventTone(event);
                return (
                  <div className="sg-fe" key={`${event.createdAt || event.time}-${i}`}>
                    <span className={`sg-fic sg-fic--${tone}`}>
                      {tone === "danger" || tone === "warn" ? (
                        <IcAlert />
                      ) : tone === "ok" ? (
                        <IcCheck />
                      ) : (
                        <IcInfo />
                      )}
                    </span>
                    <div>
                      <p>{event.message}</p>
                      <span className="ov-mono">
                        {event.time}
                        {event.action ? ` · ${event.action}` : ""}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="ov-empty">No operational events recorded.</div>
          )}
        </section>
      </div>

      {/* ── Version + cluster info ────────────────────────────── */}
      <div className="sg-row-11">
        <section className="ov-card">
          <div className="ov-card-h">
            <h3>Version Status</h3>
            <div className="ov-card-r">
              <span className={`status-pill ${versionUpToDate ? "ok" : "warn"}`}>
                {version.statusMessage || version.statusLabel || "Unknown"}
              </span>
            </div>
          </div>
          <div className="ov-kv">
            <KvRow label="Provider" value={version.provider || clusterInfo.provider || "—"} />
            <KvRow label="Current" value={version.current || "—"} mono />
            <KvRow
              label="Latest stable"
              value={formatLatestVersion(version.latest || version.latestAvailable)}
              mono
            />
            <KvRow
              label="Upgrade support"
              value={version.upgradeSupported ? "Supported" : "Not supported"}
              tone={version.upgradeSupported ? "ok" : "warn"}
            />
          </div>
          {canOpenUpgrade ? (
            <div className="ov-card-foot">
              <button type="button" className="ov-lnk" onClick={onNavigateToUpgrade}>
                Open Upgrade Safe Mode
                <IcArrow />
              </button>
            </div>
          ) : null}
        </section>

        <section className="ov-card">
          <div className="ov-card-h">
            <h3>Cluster Information</h3>
          </div>
          <div className="ov-kv">
            <KvRow label="Provider" value={clusterInfo.provider || "—"} />
            <KvRow label="Cluster name" value={clusterInfo.name || "—"} mono />
            <KvRow label="Context" value={clusterInfo.contextName || "—"} mono />
            <KvRow label="Nodes" value={clusterInfo.nodeCount ?? nodes.total} />
            <KvRow label="Namespaces" value={clusterInfo.namespaceCount ?? namespaces.length} />
            <KvRow label="Pods" value={clusterInfo.podCount ?? pods.running} />
          </div>
        </section>
      </div>
    </div>
  );
}
