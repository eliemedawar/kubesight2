import { useMemo } from "react";
import ChartCanvas from "./charts/ChartCanvas.jsx";
import Sparkline from "./charts/Sparkline.jsx";
import { cssVar, drawArea, drawLines, drawStacked } from "./charts/chartDraw.js";
import { TIME_RANGES } from "./useDashboardSeries.js";
import { formatDashboardTime, formatLatestVersion } from "../utils/dashboardStatus.js";

// Reference palette (KubeSight Operations Dashboard mock).
const TEAL = "#2dd4a7";
const PURPLE = "#8b7ff0";
const AMBER = "#f5b945";
const DANGER = "#f87171";

// Per-status accent for dots / pills / bars.
function statusColor(status) {
  const s = String(status || "").toLowerCase();
  if (s === "critical" || s === "failed" || s === "fail") return DANGER;
  if (s === "warning" || s === "warn") return AMBER;
  if (s === "healthy" || s === "passed" || s === "pass" || s === "ready") return TEAL;
  return "#5e6678";
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

// Simple trend marker from the rolling series (recent vs. a few samples back).
function trend(arr) {
  if (!arr || arr.length < 6) return null;
  const last = arr[arr.length - 1];
  const prev = arr[arr.length - 6];
  const delta = Math.round(Math.abs(last - prev));
  if (!delta) return null;
  return { dir: last >= prev ? "up" : "down", delta };
}

function KvRow({ label, value, valueColor, last }) {
  return (
    <div className={`ops-kv-row${last ? " is-last" : ""}`}>
      <span className="ops-kv-key">{label}</span>
      <span className="ops-kv-val" style={valueColor ? { color: valueColor } : undefined}>
        {value}
      </span>
    </div>
  );
}

// Faithful rebuild of the KubeSight Operations Dashboard reference, fed by the
// real dashboard summary + the rolling series hook. The reference's own
// header/sidebar are omitted — the app's AppShell already provides those.
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
  const cpu = summary?.cpuUsage || {};
  const mem = summary?.memoryUsage || {};
  const nodes = summary?.nodes || { ready: 0, total: 0, status: "unknown" };
  const pods = summary?.pods || { running: 0, pending: 0, failed: 0 };
  const alerts = summary?.alerts || { critical: 0, warning: 0, info: 0, total: 0 };
  const version = summary?.version || {};
  const clusterInfo = summary?.clusterInfo || {};
  const namespaces = summary?.namespaces || [];
  const events = useMemo(
    () => [...(summary?.operationalEvents || []), ...(summary?.recentActivity || [])].slice(0, 10),
    [summary?.operationalEvents, summary?.recentActivity]
  );

  const accent = cssVar("--accent", "#3b82f6");
  const bands = series?.cpuBands || [];
  const bandColors = bands.map((_, i) => (i === 0 ? accent : i === 1 ? TEAL : PURPLE));
  const cpuPeak = series?.cpu?.length ? Math.round(Math.max(...series.cpu)) : null;
  const netIn = series?.netIn || [];
  const netOut = series?.netOut || [];
  const maxNsPods = Math.max(1, ...namespaces.map((ns) => ns.pods || 0));

  const cpuTrend = trend(series?.cpu);
  const memTrend = trend(series?.mem);
  const healthColor = statusColor(health);

  const versionUpToDate = version.status === "up_to_date";

  return (
    <div className="ops-dash">
      {/* ── Title row ─────────────────────────────────────────── */}
      <div className="ops-titlerow">
        <div>
          <div className="ops-title-line">
            <h1 className="ops-title">Operations Dashboard</h1>
            <span className="ops-pill" style={{ "--pill": healthColor }}>
              <span className="ops-dot" />
              {statusLabel(health)}
            </span>
          </div>
          <p className="ops-subtitle">
            {clusterInfo.name || summary?.clusterId || "cluster"} · live operational view ·{" "}
            <span className="ops-mono">{version.current || clusterInfo.version || "—"}</span>
          </p>
        </div>
        <div className="ops-titlerow-actions">
          <span className="ops-updated">
            <span className="ops-dot ops-dot--teal" />
            Last updated{" "}
            <span className="ops-mono">{formatDashboardTime(lastRefreshedAt || summary?.lastUpdated)}</span>
          </span>
          <div className="ops-range" role="group" aria-label="Chart time range">
            {TIME_RANGES.map((range) => (
              <button
                key={range}
                type="button"
                className={`ops-range-pill${timeRange === range ? " is-active" : ""}`}
                aria-pressed={timeRange === range}
                onClick={() => onTimeRangeChange?.(range)}
              >
                {range}
              </button>
            ))}
          </div>
          <button type="button" className="ops-refresh" onClick={onRefresh}>
            Refresh
          </button>
        </div>
      </div>

      {/* ── KPI strip ─────────────────────────────────────────── */}
      <div className="ops-kpi-grid">
        <div className="ops-kpi">
          <div className="ops-kpi-head">
            <span className="ops-kpi-label">CLUSTER HEALTH</span>
            <span className="ops-kpi-indicator" style={{ "--pill": healthColor }} />
          </div>
          <div className="ops-kpi-value ops-kpi-value--word" style={{ color: healthColor }}>
            {statusLabel(health)}
          </div>
          <div className="ops-kpi-sub">
            {nodes.ready}/{nodes.total} nodes · {clusterInfo.podCount ?? pods.running} pods
          </div>
        </div>

        <div className="ops-kpi">
          <div className="ops-kpi-head">
            <span className="ops-kpi-label">CPU USAGE</span>
            {cpuTrend ? (
              <span className="ops-delta" style={{ color: TEAL }}>
                {cpuTrend.dir === "up" ? "▲" : "▼"} {cpuTrend.delta}%
              </span>
            ) : null}
          </div>
          <div className="ops-kpi-value">
            {cpu.available ? cpu.percent : "—"}
            <span className="ops-kpi-unit">%</span>
          </div>
          <Sparkline data={series?.cpu} color="--accent" />
          <div className="ops-kpi-sub">of {cpu.allocatableDisplay || "cluster vCPU"}</div>
        </div>

        <div className="ops-kpi">
          <div className="ops-kpi-head">
            <span className="ops-kpi-label">MEMORY USAGE</span>
            {memTrend ? (
              <span className="ops-delta" style={{ color: AMBER }}>
                {memTrend.dir === "up" ? "▲" : "▼"} {memTrend.delta}%
              </span>
            ) : null}
          </div>
          <div className="ops-kpi-value">
            {mem.available ? mem.percent : "—"}
            <span className="ops-kpi-unit">%</span>
          </div>
          <Sparkline data={series?.mem} color={PURPLE} />
          <div className="ops-kpi-sub">of {mem.allocatableDisplay || "allocatable"}</div>
        </div>

        <div className="ops-kpi">
          <div className="ops-kpi-head">
            <span className="ops-kpi-label">NODES</span>
          </div>
          <div className="ops-kpi-value">
            {nodes.ready} / {nodes.total}
          </div>
          <div className="ops-kpi-sub" style={{ color: statusColor(nodes.status) }}>
            {nodes.ready === nodes.total && nodes.total > 0 ? "All ready" : statusLabel(nodes.status)}
          </div>
        </div>

        <div className="ops-kpi">
          <div className="ops-kpi-head">
            <span className="ops-kpi-label">RUNNING PODS</span>
          </div>
          <div className="ops-kpi-value">{pods.running}</div>
          <div className="ops-kpi-sub">
            Pending {pods.pending} · Failed {pods.failed}
          </div>
        </div>

        <div className="ops-kpi">
          <div className="ops-kpi-head">
            <span className="ops-kpi-label">ACTIVE ALERTS</span>
          </div>
          <div className="ops-kpi-value" style={{ color: alerts.total > 0 ? AMBER : undefined }}>
            {alerts.total}
          </div>
          <div className="ops-kpi-sub">
            Critical {alerts.critical} · Warning {alerts.warning}
          </div>
        </div>
      </div>

      {/* ── CPU + Memory charts ───────────────────────────────── */}
      <div className="ops-chart-row">
        <section className="ops-panel">
          <div className="ops-panel-head">
            <div>
              <div className="ops-panel-title">CPU Utilization</div>
              <div className="ops-panel-subtitle">By namespace · % of cluster</div>
            </div>
            <div className="ops-panel-metric">
              <div className="ops-mono ops-panel-metric-value">{cpu.available ? `${cpu.percent}%` : "—"}</div>
              {cpuPeak != null ? <div className="ops-panel-metric-sub">peak {cpuPeak}%</div> : null}
            </div>
          </div>
          <div className="ops-legend">
            {bands.map((band, i) => (
              <span className="ops-legend-item" key={band.label}>
                <span className="ops-legend-dot" style={{ background: bandColors[i] }} />
                {band.label}
              </span>
            ))}
            {!series?.cpuReal ? <span className="ops-sample-tag">sample split</span> : null}
          </div>
          <ChartCanvas
            className="ops-chart-canvas ops-chart-canvas--tall"
            draw={(ctx, { width, height }) => {
              if (!bands.length) return;
              drawStacked(ctx, width, height, bands.map((b) => b.data), bandColors, 100, "%");
            }}
            deps={[bands, accent]}
          />
        </section>

        <section className="ops-panel">
          <div className="ops-panel-head">
            <div>
              <div className="ops-panel-title">Memory Utilization</div>
              <div className="ops-panel-subtitle">Working set · % of allocatable</div>
            </div>
            <div className="ops-panel-metric">
              <div className="ops-mono ops-panel-metric-value">{mem.available ? `${mem.percent}%` : "—"}</div>
              {mem.usedDisplay && mem.allocatableDisplay ? (
                <div className="ops-panel-metric-sub">
                  {mem.usedDisplay} / {mem.allocatableDisplay}
                </div>
              ) : null}
            </div>
          </div>
          <div className="ops-legend">
            <span className="ops-legend-item">
              <span className="ops-legend-dot" style={{ background: PURPLE }} />
              used
            </span>
            <span className="ops-legend-item">
              <span className="ops-legend-dash" />
              limit {series?.memLimit || 85}%
            </span>
          </div>
          <ChartCanvas
            className="ops-chart-canvas ops-chart-canvas--tall"
            draw={(ctx, { width, height }) => {
              if (!series?.mem?.length) return;
              drawArea(ctx, width, height, series.mem, PURPLE, 100, series.memLimit || 85, "%");
            }}
            deps={[series?.mem, series?.memLimit]}
          />
        </section>
      </div>

      {/* ── Network (full width) ──────────────────────────────── */}
      <section className="ops-panel">
        <div className="ops-panel-head">
          <div>
            <div className="ops-panel-title">Network I/O</div>
            <div className="ops-panel-subtitle">Cluster-wide throughput</div>
          </div>
          <div className="ops-net-metrics">
            <div className="ops-net-metric">
              <div className="ops-net-legend">
                <span className="ops-legend-dot" style={{ background: accent }} />
                Ingress
              </div>
              <div className="ops-mono ops-net-value">{fmtThroughput(netIn[netIn.length - 1])}</div>
            </div>
            <div className="ops-net-metric">
              <div className="ops-net-legend">
                <span className="ops-legend-dot" style={{ background: TEAL }} />
                Egress
              </div>
              <div className="ops-mono ops-net-value">{fmtThroughput(netOut[netOut.length - 1])}</div>
            </div>
            {!series?.netReal ? <span className="ops-sample-tag">sample</span> : null}
          </div>
        </div>
        <ChartCanvas
          className="ops-chart-canvas"
          draw={(ctx, { width, height }) => {
            if (!netIn.length || !netOut.length) return;
            drawLines(ctx, width, height, [netIn, netOut], [accent, TEAL], 1600, " KB");
          }}
          deps={[netIn, netOut, accent]}
        />
      </section>

      {/* ── Namespace health + events ─────────────────────────── */}
      <div className="ops-grid ops-grid--wide-left">
        <section className="ops-panel ops-panel--flush">
          <div className="ops-panel-bar">
            <div className="ops-panel-title">Namespace Health</div>
            <span className="ops-panel-count">{namespaces.length} namespaces</span>
          </div>
          <div className="ops-table-head">
            <span>NAMESPACE</span>
            <span>STATUS</span>
            <span>PODS</span>
            <span>SHARE</span>
          </div>
          {namespaces.length ? (
            namespaces.map((ns) => (
              <div className="ops-table-row" key={ns.name}>
                <span className="ops-mono ops-table-name">{ns.name}</span>
                <span className="ops-status" style={{ color: statusColor(ns.status) }}>
                  <span className="ops-dot" style={{ "--pill": statusColor(ns.status) }} />
                  {statusLabel(ns.status)}
                </span>
                <span className="ops-mono ops-table-pods">{ns.pods}</span>
                <span className="ops-bar">
                  <span
                    className="ops-bar-fill"
                    style={{
                      width: `${Math.round(((ns.pods || 0) / maxNsPods) * 100)}%`,
                      background: statusColor(ns.status),
                    }}
                  />
                </span>
              </div>
            ))
          ) : (
            <div className="ops-empty">No namespaces available for this cluster.</div>
          )}
        </section>

        <section className="ops-panel ops-panel--flush ops-panel--col">
          <div className="ops-panel-bar">
            <div className="ops-panel-title">Events &amp; Alerts</div>
            <button type="button" className="ops-link" onClick={onViewAllEvents}>
              View all →
            </button>
          </div>
          <div className="ops-events">
            {events.length ? (
              events.map((event, i) => {
                const tone = /fail|error|critical/i.test(event.action || event.message || "")
                  ? DANGER
                  : /warn/i.test(event.action || event.message || "")
                    ? AMBER
                    : accent;
                return (
                  <div className="ops-event" key={`${event.createdAt || event.time}-${i}`}>
                    <span className="ops-event-dot" style={{ "--pill": tone }} />
                    <div className="ops-event-body">
                      <div className="ops-event-msg">{event.message}</div>
                      <div className="ops-event-meta ops-mono">
                        {event.time}
                        {event.action ? ` · ${event.action}` : ""}
                      </div>
                    </div>
                  </div>
                );
              })
            ) : (
              <div className="ops-empty">No operational events recorded.</div>
            )}
          </div>
        </section>
      </div>

      {/* ── Version + cluster info ────────────────────────────── */}
      <div className="ops-grid ops-grid--even">
        <section className="ops-panel">
          <div className="ops-panel-head">
            <div className="ops-panel-title">Version Status</div>
            <span
              className="ops-pill ops-pill--sm"
              style={{ "--pill": versionUpToDate ? TEAL : AMBER }}
            >
              {version.statusMessage || version.statusLabel || "Unknown"}
            </span>
          </div>
          <div className="ops-kv">
            <KvRow label="PROVIDER" value={version.provider || clusterInfo.provider || "—"} />
            <KvRow label="CURRENT" value={version.current || "—"} />
            <KvRow label="LATEST STABLE" value={formatLatestVersion(version.latest || version.latestAvailable)} />
            <KvRow
              label="UPGRADE SUPPORT"
              value={version.upgradeSupported ? "Supported" : "Not supported"}
              valueColor={version.upgradeSupported ? TEAL : AMBER}
              last
            />
          </div>
          {canOpenUpgrade ? (
            <button type="button" className="ops-upgrade-btn" onClick={onNavigateToUpgrade}>
              Open Upgrade Safe Mode →
            </button>
          ) : null}
        </section>

        <section className="ops-panel">
          <div className="ops-panel-head">
            <div className="ops-panel-title">Cluster Information</div>
          </div>
          <div className="ops-kv">
            <KvRow label="PROVIDER" value={clusterInfo.provider || "—"} />
            <KvRow label="CLUSTER NAME" value={clusterInfo.name || "—"} />
            <KvRow label="CONTEXT" value={clusterInfo.contextName || "—"} />
            <KvRow label="NODES" value={clusterInfo.nodeCount ?? nodes.total} />
            <KvRow label="NAMESPACES" value={clusterInfo.namespaceCount ?? namespaces.length} />
            <KvRow label="PODS" value={clusterInfo.podCount ?? pods.running} last />
          </div>
        </section>
      </div>
    </div>
  );
}
