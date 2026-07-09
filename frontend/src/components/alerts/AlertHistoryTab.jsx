import { useMemo, useState } from "react";
import DataTable from "../common/DataTable.jsx";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import LoadingState from "../common/LoadingState.jsx";
import {
  formatDurationShort,
  parseAlertTime,
  resolvedStats,
  severityInfo,
} from "../../lib/alertFeed.js";

const RANGES = [
  { key: "24h", label: "24 h", ms: 24 * 3600000 },
  { key: "7d", label: "7 d", ms: 7 * 24 * 3600000 },
  { key: "30d", label: "30 d", ms: 30 * 24 * 3600000 },
];

const HISTORY_COLUMNS = [
  { key: "severity", label: "Severity" },
  { key: "alert", label: "Alert" },
  { key: "scope", label: "Scope" },
  { key: "fired", label: "Fired" },
  { key: "status", label: "Status" },
  { key: "ttr", label: "Time to resolve" },
];

function timeToResolve(row) {
  if (String(row.status || "").toLowerCase() !== "resolved") {
    return "—";
  }
  const firedTs = parseAlertTime(row.firedAt);
  const resolvedTs = parseAlertTime(row.resolvedAt);
  if (Number.isNaN(firedTs) || Number.isNaN(resolvedTs) || resolvedTs < firedTs) {
    return "—";
  }
  return formatDurationShort(resolvedTs - firedTs);
}

/**
 * Alert history beside the live feed it explains: fired/resolved rows over a
 * selectable range, with time-to-resolve. Rows open the same detail drawer as
 * the feed.
 */
export default function AlertHistoryTab({ items, loading, error, nowTs, onOpenAlert }) {
  const [range, setRange] = useState("24h");

  const rangeMs = RANGES.find((entry) => entry.key === range)?.ms || RANGES[0].ms;

  const filtered = useMemo(
    () =>
      items.filter((row) => {
        const ts = parseAlertTime(row.firedAt);
        return !Number.isNaN(ts) && nowTs - ts < rangeMs;
      }),
    [items, nowTs, rangeMs]
  );

  const stats = useMemo(
    () => resolvedStats(items, { nowTs, windowMs: rangeMs }),
    [items, nowTs, rangeMs]
  );

  const rows = useMemo(
    () =>
      filtered.map((row) => {
        const severity = severityInfo(row.severity);
        return {
          id: row.id,
          severity: (
            <span className={`status-pill ${severity.tone}`}>{severity.label}</span>
          ),
          alert: (
            <span className="al-hist-title">
              <b>{row.title || row.policyName || "Alert"}</b>
              {row.policyName && row.title && row.policyName !== row.title ? (
                <span>{row.policyName}</span>
              ) : null}
            </span>
          ),
          scope: (
            <span className="al-mono">
              {[row.namespace ? `ns/${row.namespace}` : "", row.resourceName || row.pod || ""]
                .filter(Boolean)
                .join(" · ") || "—"}
            </span>
          ),
          fired: row.firedAt ? new Date(parseAlertTime(row.firedAt)).toLocaleString() : "—",
          /* Pre-rendered pill: DataTable's generic tone map paints "active"
             green (healthy), which misreads for a still-firing alert. */
          status:
            row.status === "active" ? (
              <span className="status-pill info">active</span>
            ) : (
              <span className="status-pill ok">resolved</span>
            ),
          ttr: <span className="al-mono">{timeToResolve(row)}</span>,
          _alert: row,
        };
      }),
    [filtered]
  );

  return (
    <div className="al-history">
      <div className="al-toolbar">
        <div className="al-seg" role="group" aria-label="History range">
          {RANGES.map((entry) => (
            <button
              key={entry.key}
              type="button"
              aria-pressed={range === entry.key}
              onClick={() => setRange(entry.key)}
            >
              {entry.label}
            </button>
          ))}
        </div>
        <span className="al-count">
          {filtered.length} alert{filtered.length === 1 ? "" : "s"}
          {stats.count && stats.medianMs != null
            ? ` · median time-to-resolve ${formatDurationShort(stats.medianMs)}`
            : ""}
        </span>
      </div>

      {error ? <ErrorBanner message={error} /> : null}
      {loading && !items.length ? (
        <LoadingState label="Loading alert history..." />
      ) : rows.length ? (
        <DataTable
          tableClassName="alert-history-table"
          columns={HISTORY_COLUMNS}
          rows={rows}
          onRowClick={onOpenAlert ? (row) => onOpenAlert(row._alert) : undefined}
        />
      ) : !error ? (
        <EmptyState
          message="No alert history in this range."
          hint="Fired policies will appear here, including auto-resolved service alerts."
        />
      ) : null}
    </div>
  );
}
