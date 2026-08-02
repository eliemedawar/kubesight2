// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import PageTitle from "./PageTitle.jsx";
import PageHeader from "./PageHeader.jsx";
import { ROUTES } from "../../routes/routeTable.js";

/**
 * Breadcrumbs are a stated requirement of the routing work. They are derived
 * from the route rather than passed in, so the thing worth testing is that
 * every page which renders a heading gets them — not that one page does.
 */

afterEach(cleanup);

const renderAt = (path, element) =>
  render(<MemoryRouter initialEntries={[path]}>{element}</MemoryRouter>);

const trail = () =>
  within(screen.getByRole("navigation", { name: /breadcrumb/i }))
    .getAllByRole("listitem")
    .map((li) => li.textContent.trim())
    .filter((text) => text !== "/");

describe("breadcrumbs come from the route", () => {
  it("shows the group and the page", () => {
    renderAt("/admin/audit", <PageTitle title="Audit Logs" />);
    expect(trail()).toEqual(["Administration", "Audit Logs"]);
  });

  it("shows the parent chain on a drill-down", () => {
    renderAt("/applications/42", <PageTitle title="payments-api" />);
    expect(trail()).toEqual(["Applications", "Inventory", "payments-api"]);
  });

  it("links the ancestors and not the current page", () => {
    renderAt("/applications/42", <PageTitle title="payments-api" />);
    const nav = screen.getByRole("navigation", { name: /breadcrumb/i });
    expect(within(nav).getByRole("link", { name: "Inventory" })).toHaveAttribute(
      "href",
      "/applications"
    );
    expect(within(nav).queryByRole("link", { name: "payments-api" })).toBeNull();
  });

  it("uses the page's own title as the last crumb", () => {
    renderAt("/fleet/clusters/prod-eu", <PageTitle title="prod-eu" />);
    expect(trail().at(-1)).toBe("prod-eu");
  });

  // Every route sits in a nav group, so even a top-level page gets the group
  // as context: "Home / Dashboard" is mildly redundant, and a special case for
  // one route would be worse than the consistency.
  it("still names the group on a top-level page", () => {
    renderAt("/", <PageTitle title="Dashboard" />);
    expect(trail()).toEqual(["Home", "Dashboard"]);
  });

  it("can be opted out of", () => {
    renderAt("/admin/audit", <PageTitle title="Audit Logs" breadcrumbs={false} />);
    expect(screen.queryByRole("navigation", { name: /breadcrumb/i })).toBeNull();
  });

  it("renders nothing on an unrouted path rather than guessing", () => {
    renderAt("/nope", <PageTitle title="Whatever" />);
    expect(screen.queryByRole("navigation", { name: /breadcrumb/i })).toBeNull();
  });
});

describe("every route can render a heading", () => {
  const paths = ROUTES.map((route) => [
    route.pageKey,
    route.path
      .replace(":clusterId", "prod-eu")
      .replace(":namespace", "payments")
      .replace(":applicationId", "42")
      .replace(":provider", "jira"),
  ]);

  it.each(paths)("%s", (pageKey, path) => {
    renderAt(path, <PageTitle title="Heading" subtitle="Sub" />);
    expect(screen.getByRole("heading", { name: "Heading" })).toBeInTheDocument();
    // No crumb may render as blank or as an unresolved param.
    const nav = screen.queryByRole("navigation", { name: /breadcrumb/i });
    if (nav) {
      within(nav)
        .getAllByRole("listitem")
        .forEach((li) => {
          expect(li.textContent.trim()).not.toBe("");
          expect(li.textContent).not.toMatch(/:/);
        });
    }
  });
});

describe("PageHeader is the same component", () => {
  // They were briefly two implementations of one block, which is the
  // duplication this layer exists to remove.
  it("renders the same heading and trail", () => {
    renderAt("/admin/audit", <PageHeader title="Audit Logs" />);
    expect(screen.getByRole("heading", { name: "Audit Logs" })).toBeInTheDocument();
    expect(trail()).toEqual(["Administration", "Audit Logs"]);
  });

  it("still honours the older showBreadcrumbs prop", () => {
    renderAt("/admin/audit", <PageHeader title="Audit Logs" showBreadcrumbs={false} />);
    expect(screen.queryByRole("navigation", { name: /breadcrumb/i })).toBeNull();
  });
});
