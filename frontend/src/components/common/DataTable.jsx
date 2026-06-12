import { isValidElement, memo, useMemo, useState } from "react";

const PAGE_SIZE_DEFAULT = 50;

function renderCell(colKey, value, truncate) {
  if (value == null) return "-";
  if (isValidElement(value)) return value;

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
}

// Memoised row — only re-renders when its own data changes
const TableRow = memo(function TableRow({ row, columns, onRowClick, truncateCells }) {
  return (
    <tr
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
  );
});

export default function DataTable({
  columns,
  rows,
  onRowClick,
  tableClassName = "",
  pageSize = PAGE_SIZE_DEFAULT,
}) {
  const [page, setPage] = useState(0);

  const truncateCells =
    tableClassName.includes("resources-table") || tableClassName.includes("alert-policies-table");

  const totalPages = Math.ceil(rows.length / pageSize);
  const paginated = useMemo(
    () => rows.slice(page * pageSize, page * pageSize + pageSize),
    [rows, page, pageSize]
  );

  // Reset to page 0 when the row set changes (e.g. after a filter or poll update)
  const rowCount = rows.length;
  useMemo(() => { setPage(0); }, [rowCount]); // eslint-disable-line react-hooks/exhaustive-deps

  const showPager = rows.length > pageSize;

  return (
    <div>
      <div className="table-shell table-scroll-region" role="region" aria-label="Scrollable table" tabIndex={0}>
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
            {paginated.map((row, index) => (
              <TableRow
                key={`${row.id || row.name || row.item || "row"}-${page * pageSize + index}`}
                row={row}
                columns={columns}
                onRowClick={onRowClick}
                truncateCells={truncateCells}
              />
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

      {showPager && (
        <div className="data-table-pager">
          <button
            className="btn-ghost data-table-pager__btn"
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
          >
            ← Prev
          </button>
          <span className="data-table-pager__info">
            {page * pageSize + 1}–{Math.min((page + 1) * pageSize, rows.length)} of {rows.length}
          </span>
          <button
            className="btn-ghost data-table-pager__btn"
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}
