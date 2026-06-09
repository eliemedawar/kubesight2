import {
  IconDocument,
  IconExpand,
  IconEye,
  IconHistory,
  IconRefresh,
  IconUpgrade,
  InventoryIconButton,
} from "./InventoryActionIcons.jsx";

const EMPTY_PLACEHOLDERS = new Set(["", "unassigned", "not set", "-", "—"]);

function formatDateShort(value) {
  if (!value) return "—";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return String(value);
  }
}

export function formatInventoryEmpty(value) {
  const text = String(value ?? "").trim();
  if (!text || EMPTY_PLACEHOLDERS.has(text.toLowerCase())) {
    return "—";
  }
  if (text.toLowerCase().includes("metrics unavailable")) {
    return text;
  }
  return text;
}

function Truncate({ value, className = "" }) {
  const text = value == null || value === "" ? "—" : String(value);
  return (
    <span className={`inventory-cell-truncate ${className}`.trim()} title={text !== "—" ? text : undefined}>
      {text}
    </span>
  );
}

function StatusPill({ status }) {
  const text = String(status || "Unknown");
  const toneMap = {
    healthy: "ok",
    warning: "warn",
    critical: "danger",
  };
  const tone = toneMap[text.toLowerCase()] || "info";
  return (
    <span className={`status-pill status-pill--compact ${tone}`} title={text}>
      {text}
    </span>
  );
}

function stopRowClick(event) {
  event.stopPropagation();
}

export default function InventoryHubTable({ rows, onRowClick, renderActions }) {
  return (
    <div className="inventory-table-scroll">
      <table className="inventory-hub-table">
        <thead>
          <tr>
            <th className="col-app">Application</th>
            <th className="col-cluster">Cluster</th>
            <th className="col-namespace">Namespace</th>
            <th className="col-status">Health</th>
            <th className="col-type">Type</th>
            <th className="col-version">Version</th>
            <th className="col-image">Image</th>
            <th className="col-replicas">Pods</th>
            <th className="col-owner">Owner</th>
            <th className="col-updated">Updated</th>
            <th className="col-actions">Actions</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={`${row.id || row.name}-${index}`}
              className="inventory-hub-row data-table-row--clickable"
              onClick={() => onRowClick?.(row)}
              onKeyDown={(event) => {
                if (onRowClick && (event.key === "Enter" || event.key === " ")) {
                  event.preventDefault();
                  onRowClick(row);
                }
              }}
              tabIndex={onRowClick ? 0 : undefined}
              role={onRowClick ? "button" : undefined}
            >
              <td className="col-app">
                <Truncate value={row.name} className="inventory-cell-truncate--strong" />
              </td>
              <td className="col-cluster">
                <Truncate value={row.clusterName || row.cluster} />
              </td>
              <td className="col-namespace">
                <Truncate value={row.namespace} />
              </td>
              <td className="col-status">
                <StatusPill status={row.status} />
              </td>
              <td className="col-type">
                <Truncate value={row.workloadType} />
              </td>
              <td className="col-version">
                <Truncate value={row.versionTag} />
              </td>
              <td className="col-image">
                <Truncate value={row.image} />
              </td>
              <td className="col-replicas">
                <Truncate value={row.replicasDisplay} />
              </td>
              <td className="col-owner">
                <Truncate value={formatInventoryEmpty(row.ownerTeam)} />
              </td>
              <td className="col-updated">
                <Truncate value={formatDateShort(row.lastUpdated)} />
              </td>
              <td className="col-actions" onClick={stopRowClick} onKeyDown={stopRowClick}>
                {renderActions(row)}
              </td>
            </tr>
          ))}
          {!rows.length ? (
            <tr>
              <td colSpan={11} className="muted">
                No data available.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

export function InventoryRowIconActions({
  showLogs,
  showDeployOps,
  showHelmUpgrade,
  showHelmRollback,
  onView,
  onLogs,
  onRestart,
  onScale,
  onHelmUpgrade,
  onHelmRollback,
}) {
  return (
    <div className="inventory-actions-cell inventory-actions-cell--icons">
      <InventoryIconButton label="View details" onClick={(e) => { e.stopPropagation(); onView(); }}>
        <IconEye />
      </InventoryIconButton>
      {showLogs ? (
        <InventoryIconButton label="View logs" onClick={(e) => { e.stopPropagation(); onLogs(); }}>
          <IconDocument />
        </InventoryIconButton>
      ) : null}
      {showDeployOps ? (
        <InventoryIconButton label="Restart deployment" onClick={(e) => { e.stopPropagation(); onRestart(); }}>
          <IconRefresh />
        </InventoryIconButton>
      ) : null}
      {showDeployOps ? (
        <InventoryIconButton label="Scale deployment" onClick={(e) => { e.stopPropagation(); onScale(); }}>
          <IconExpand />
        </InventoryIconButton>
      ) : null}
      {showHelmUpgrade ? (
        <InventoryIconButton label="Helm upgrade" onClick={(e) => { e.stopPropagation(); onHelmUpgrade(); }}>
          <IconUpgrade />
        </InventoryIconButton>
      ) : null}
      {showHelmRollback ? (
        <InventoryIconButton label="Helm rollback" onClick={(e) => { e.stopPropagation(); onHelmRollback(); }}>
          <IconHistory />
        </InventoryIconButton>
      ) : null}
    </div>
  );
}
