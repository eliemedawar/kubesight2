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
    imagepullbackoff: "danger",
    errimagepull: "danger",
    errimagepullbackoff: "danger",
    createcontainererror: "danger",
    createcontainerconfigerror: "danger",
    invalidimagename: "danger",
    oomkilled: "danger",
    evicted: "danger",
    failed: "danger",
    notready: "warn",
    terminating: "warn",
    containercreating: "warn",
    podinitializing: "warn",
    completed: "ok",
    succeeded: "ok",
    scaling: "info",
    low: "info",
  };

  const lower = text.toLowerCase();
  let tone = toneMap[lower];
  if (!tone) {
    // Fall back to a heuristic so any kubectl reason (incl. "Init:..." and
    // "ExitCode:N") still gets a sensible colour instead of neutral blue.
    if (/(err|crash|backoff|oom|evicted|fail|error|exitcode|signal)/.test(lower)) {
      tone = "danger";
    } else if (/(init:|pending|terminating|creating|notready|podinitializing|scaling)/.test(lower)) {
      tone = "warn";
    } else {
      tone = "info";
    }
  }
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

/**
 * Search and sort operate on a column's `sortValue(row)` when given, and
 * otherwise on `row[col.key]`. Explicit rather than inferred from the rendered
 * cell, because a cell that renders a pill or a link has no meaningful text to
 * compare, and guessing produces a sort that is subtly wrong rather than
 * absent — the worse failure, since nothing looks broken.
 */
function cellValue(row, col) {
  if (typeof col.sortValue === "function") {
    return col.sortValue(row);
  }
  const raw = row?.[col.key];
  return raw == null ? "" : raw;
}

function compare(a, b) {
  if (typeof a === "number" && typeof b === "number") {
    return a - b;
  }
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

export default function DataTable({
  columns,
  rows,
  onRowClick,
  tableClassName = "",
  pageSize = PAGE_SIZE_DEFAULT,
  searchable = false,
  searchPlaceholder = "Search…",
  searchLabel = "Search table",
  emptyMessage = "No data available.",
  toolbar = null,
}) {
  const [page, setPage] = useState(0);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ key: "", direction: "asc" });

  const truncateCells =
    tableClassName.includes("resources-table") || tableClassName.includes("alert-policies-table");

  const searchableColumns = useMemo(
    () => columns.filter((col) => col.searchable !== false),
    [columns]
  );

  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) {
      return rows;
    }
    return rows.filter((row) =>
      searchableColumns.some((col) => String(cellValue(row, col)).toLowerCase().includes(term))
    );
  }, [rows, query, searchableColumns]);

  const sorted = useMemo(() => {
    if (!sort.key) {
      return filtered;
    }
    const col = columns.find((entry) => entry.key === sort.key);
    if (!col) {
      return filtered;
    }
    // Copy before sorting: `rows` belongs to the caller, and sorting it in place
    // would reorder their state as a side effect of rendering.
    const next = [...filtered];
    next.sort((a, b) => {
      const result = compare(cellValue(a, col), cellValue(b, col));
      return sort.direction === "asc" ? result : -result;
    });
    return next;
  }, [filtered, sort, columns]);

  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = useMemo(
    () => sorted.slice(page * pageSize, page * pageSize + pageSize),
    [sorted, page, pageSize]
  );

  // Reset to page 0 when the row set changes (e.g. after a filter or poll update)
  const rowCount = sorted.length;
  useMemo(() => { setPage(0); }, [rowCount]); // eslint-disable-line react-hooks/exhaustive-deps

  const showPager = sorted.length > pageSize;

  const toggleSort = (key) => {
    setSort((current) =>
      current.key === key
        ? { key, direction: current.direction === "asc" ? "desc" : "asc" }
        : { key, direction: "asc" }
    );
  };

  return (
    <div>
      {searchable || toolbar ? (
        <div className="data-table-toolbar">
          {searchable ? (
            <input
              type="search"
              className="data-table-search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={searchPlaceholder}
              aria-label={searchLabel}
            />
          ) : null}
          {toolbar}
        </div>
      ) : null}

      <div className="table-shell table-scroll-region" role="region" aria-label="Scrollable table" tabIndex={0}>
        <table className={tableClassName || undefined}>
          <thead>
            <tr>
              {columns.map((col) => {
                const isSorted = sort.key === col.key;
                if (!col.sortable) {
                  return (
                    <th key={col.key} className={`col-${col.key}`}>
                      {col.label}
                    </th>
                  );
                }
                return (
                  <th
                    key={col.key}
                    className={`col-${col.key} is-sortable`}
                    aria-sort={isSorted ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}
                  >
                    <button
                      type="button"
                      className="data-table-sort"
                      onClick={() => toggleSort(col.key)}
                    >
                      {col.label}
                      <span className="data-table-sort-marker" aria-hidden="true">
                        {isSorted ? (sort.direction === "asc" ? "▲" : "▼") : "↕"}
                      </span>
                    </button>
                  </th>
                );
              })}
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
            {!sorted.length ? (
              <tr>
                <td colSpan={columns.length} className="muted">
                  {/*
                    "No matches" and "no data" are different facts, and telling
                    an operator there is nothing here when their search is the
                    reason sends them looking for a data problem.
                  */}
                  {query.trim() ? `No rows match “${query.trim()}”.` : emptyMessage}
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
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z" clipRule="evenodd" /></svg> Prev
          </button>
          <span className="data-table-pager__info">
            {/* Counts follow the filter, so the pager describes what is on screen. */}
            {page * pageSize + 1}–{Math.min((page + 1) * pageSize, sorted.length)} of {sorted.length}
          </span>
          <button
            className="btn-ghost data-table-pager__btn"
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
          >
            Next <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fillRule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clipRule="evenodd" /></svg>
          </button>
        </div>
      )}
    </div>
  );
}
