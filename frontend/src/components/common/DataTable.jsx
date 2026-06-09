import { isValidElement } from "react";

export default function DataTable({ columns, rows, onRowClick, tableClassName = "" }) {
  const renderCell = (colKey, value, truncate) => {
    if (value == null) {
      return "-";
    }
    if (isValidElement(value)) {
      return value;
    }

    const text = String(value);
    const isStatusField = ["status", "state", "severity", "updateStatus"].includes(colKey);

    if (!isStatusField) {
      if (truncate) {
        return (
          <span className="table-cell-truncate" title={text !== "-" ? text : undefined}>
            {text}
          </span>
        );
      }
      return text;
    }

    const toneMap = {
      healthy: "ok",
      warning: "warn",
      critical: "danger",
      unknown: "info",
      active: "ok",
      connected: "ok",
      running: "ok",
      done: "ok",
      stable: "ok",
      configured: "ok",
      enabled: "ok",
      disabled: "info",
      paused: "warn",
      "needs setup": "warn",
      monitoring: "warn",
      pending: "warn",
      draft: "warn",
      medium: "warn",
      high: "danger",
      danger: "danger",
      error: "danger",
      firing: "danger",
      triggered: "danger",
      crashloopbackoff: "danger",
      scaling: "info",
      low: "info",
    };

    const tone = toneMap[text.toLowerCase()] || "info";
    return <span className={`status-pill ${tone}`}>{text}</span>;
  };

  const truncateCells = tableClassName.includes("resources-table");

  return (
    <div className="table-shell">
      <table className={tableClassName || undefined}>
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.key} className={`col-${col.key}`}>
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr
              key={`${row.id || row.name || row.item || "row"}-${index}`}
              className={onRowClick ? "data-table-row--clickable" : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={
                onRowClick
                  ? (event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
              tabIndex={onRowClick ? 0 : undefined}
              role={onRowClick ? "button" : undefined}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className={`col-${col.key}${col.key === "actions" ? " col-actions" : ""}`}
                >
                  {renderCell(col.key, row[col.key], truncateCells && col.key !== "actions")}
                </td>
              ))}
            </tr>
          ))}
          {!rows.length ? (
            <tr>
              <td colSpan={columns.length} className="muted">
                No data available.
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
