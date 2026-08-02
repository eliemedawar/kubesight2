// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import DataTable from "./DataTable.jsx";

afterEach(cleanup);

const COLUMNS = [
  { key: "name", label: "Name", sortable: true },
  { key: "count", label: "Count", sortable: true },
  { key: "status", label: "Status" },
];

const ROWS = [
  { id: "1", name: "payments", count: 10, status: "ok" },
  { id: "2", name: "Billing", count: 2, status: "degraded" },
  { id: "3", name: "search", count: 100, status: "ok" },
];

const bodyText = () =>
  within(screen.getByRole("table"))
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.textContent);

/** First cell of each body row, so assertions ignore the other columns. */
const names = () =>
  within(screen.getByRole("table"))
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent.trim());

describe("search", () => {
  it("filters rows across searchable columns", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} searchable />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "bill" } });
    expect(bodyText()).toHaveLength(1);
    expect(bodyText()[0]).toContain("Billing");
  });

  it("is case-insensitive", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} searchable />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "PAYMENTS" } });
    expect(bodyText()).toHaveLength(1);
  });

  // "No matches" and "no data" are different facts; conflating them sends the
  // operator looking for a data problem that is really their own filter.
  it("says the search matched nothing, not that there is no data", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} searchable emptyMessage="No integrations yet." />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "zzz" } });
    expect(screen.getByText(/No rows match/)).toBeInTheDocument();
    expect(screen.queryByText("No integrations yet.")).toBeNull();
  });

  it("shows the caller's empty message when there is genuinely no data", () => {
    render(<DataTable columns={COLUMNS} rows={[]} searchable emptyMessage="No integrations yet." />);
    expect(screen.getByText("No integrations yet.")).toBeInTheDocument();
  });

  it("renders no search box unless asked", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    expect(screen.queryByRole("searchbox")).toBeNull();
  });

  it("skips columns marked unsearchable", () => {
    const columns = [...COLUMNS.slice(0, 2), { key: "status", label: "Status", searchable: false }];
    render(<DataTable columns={columns} rows={ROWS} searchable />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "degraded" } });
    expect(screen.getByText(/No rows match/)).toBeInTheDocument();
  });
});

describe("sort", () => {
  it("sorts ascending then descending on repeat clicks", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    const header = screen.getByRole("button", { name: /Name/ });

    fireEvent.click(header);
    expect(names()).toEqual(["Billing", "payments", "search"]);

    fireEvent.click(header);
    expect(names()).toEqual(["search", "payments", "Billing"]);
  });

  it("compares numbers numerically, not as strings", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    fireEvent.click(screen.getByRole("button", { name: /Count/ }));
    expect(bodyText().map((t) => t.match(/\d+/)[0])).toEqual(["2", "10", "100"]);
  });

  it("exposes sort direction to assistive technology", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    const nameHeader = screen.getByRole("columnheader", { name: /Name/ });
    expect(nameHeader).toHaveAttribute("aria-sort", "none");

    fireEvent.click(screen.getByRole("button", { name: /Name/ }));
    expect(nameHeader).toHaveAttribute("aria-sort", "ascending");
  });

  it("leaves unsortable columns without a control", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    expect(screen.queryByRole("button", { name: /Status/ })).toBeNull();
  });

  // The caller's array is their state; sorting it in place would reorder their
  // data as a side effect of rendering.
  it("does not mutate the rows it was given", () => {
    const rows = [...ROWS];
    const snapshot = rows.map((row) => row.id);
    render(<DataTable columns={COLUMNS} rows={rows} />);
    fireEvent.click(screen.getByRole("button", { name: /Name/ }));
    expect(rows.map((row) => row.id)).toEqual(snapshot);
  });
});

describe("search and sort together", () => {
  it("sorts within the filtered set and counts what is on screen", () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
      id: String(i),
      name: `svc-${i}`,
      count: 12 - i,
      status: "ok",
    }));
    render(<DataTable columns={COLUMNS} rows={many} searchable pageSize={5} />);

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "svc-1" } });
    // svc-1, svc-10, svc-11 -- fits on one page, so the pager goes away.
    expect(bodyText()).toHaveLength(3);
    expect(screen.queryByText(/of 12/)).toBeNull();
  });
});
