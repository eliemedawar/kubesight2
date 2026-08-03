// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getVisiblePages } from "../../utils/authz.js";
import Sidebar from "./Sidebar.jsx";

/**
 * The rework is behavioural, so these assert behaviour: that hovering does
 * nothing, that clicking does everything, and that the group you are in cannot
 * end up collapsed.
 */

afterEach(cleanup);

const ADMIN = { isAdmin: true, hasFullAccess: true, role: "admin", permissions: [], accessRules: [] };
const PAGES = getVisiblePages(ADMIN);

function renderSidebar({ activePage = "dashboard", onNavigated, path } = {}) {
  return render(
    <MemoryRouter initialEntries={path ? [path] : undefined}>
      <Sidebar pages={PAGES} activePage={activePage} onNavigated={onNavigated} open />
    </MemoryRouter>
  );
}

const activeLinks = () =>
  screen
    .getAllByRole("link")
    .filter((link) => link.className.includes("active"))
    .map((link) => link.textContent.trim());

const groupButton = (label) => screen.getByRole("button", { name: label });
const groupPanel = (label) => screen.getByRole("group", { name: `${label} pages` });
const isExpanded = (label) => groupButton(label).getAttribute("aria-expanded") === "true";

describe("hover does nothing", () => {
  // The complaint that drove the rework. The previous version opened groups on
  // pointer enter after a 160ms delay and closed them 140ms after leaving.
  it("does not open a group on pointer enter", () => {
    renderSidebar();
    expect(isExpanded("Changes")).toBe(false);

    fireEvent.pointerEnter(groupButton("Changes"));
    fireEvent.mouseOver(groupButton("Changes"));
    expect(isExpanded("Changes")).toBe(false);
  });

  it("does not close an open group on pointer leave", () => {
    renderSidebar();
    fireEvent.click(groupButton("Changes"));
    expect(isExpanded("Changes")).toBe(true);

    fireEvent.pointerLeave(groupButton("Changes"));
    fireEvent.mouseOut(groupButton("Changes"));
    expect(isExpanded("Changes")).toBe(true);
  });

  it("does not open a group merely because it received focus", () => {
    renderSidebar();
    fireEvent.focus(groupButton("Applications"));
    expect(isExpanded("Applications")).toBe(false);
  });
});

describe("click controls expansion", () => {
  it("opens a closed group and closes it again", () => {
    renderSidebar();
    expect(isExpanded("Changes")).toBe(false);

    fireEvent.click(groupButton("Changes"));
    expect(isExpanded("Changes")).toBe(true);

    fireEvent.click(groupButton("Changes"));
    expect(isExpanded("Changes")).toBe(false);
  });

  // Expansion is the only signal the sidebar has for "current section", so it
  // has to be spent on exactly one group. With three open it degrades from
  // "this is where you are" to "this is where you have been".
  it("opens one group at a time", () => {
    renderSidebar();
    fireEvent.click(groupButton("Changes"));
    expect(isExpanded("Changes")).toBe(true);

    fireEvent.click(groupButton("Applications"));
    expect(isExpanded("Applications")).toBe(true);
    expect(isExpanded("Changes")).toBe(false);
  });

  it("never leaves every group collapsed", () => {
    renderSidebar({ activePage: "dashboard" });
    fireEvent.click(groupButton("Changes"));
    // Collapsing the group you were browsing falls back to the one you are in,
    // rather than to nothing at all.
    fireEvent.click(groupButton("Changes"));

    expect(isExpanded("Changes")).toBe(false);
    expect(isExpanded("Home")).toBe(true);
  });
});

describe("the active group stays expanded", () => {
  it("starts expanded on the group containing the current page", () => {
    renderSidebar({ activePage: "changeBundles" });
    expect(isExpanded("Changes")).toBe(true);
  });

  it("cannot be collapsed while you are inside it", () => {
    renderSidebar({ activePage: "changeBundles" });
    fireEvent.click(groupButton("Changes"));
    expect(isExpanded("Changes")).toBe(true);
  });

  it("expands the group you navigate into, and closes the one you left", () => {
    const { rerender } = renderSidebar({ activePage: "dashboard" });
    expect(isExpanded("Home")).toBe(true);
    expect(isExpanded("Applications")).toBe(false);

    rerender(
      <MemoryRouter>
        <Sidebar pages={PAGES} activePage="inventory" open />
      </MemoryRouter>
    );
    expect(isExpanded("Applications")).toBe(true);
    expect(isExpanded("Home")).toBe(false);
  });

  // Drill-downs highlight through their parent, so opening an application
  // detail page must not leave the menu looking like nothing is selected.
  it("keeps the parent group open on a drill-down", () => {
    renderSidebar({ activePage: "inventory" });
    expect(isExpanded("Applications")).toBe(true);
  });
});

describe("exactly one entry is marked current", () => {
  // The reported bug: on /applications/clients, both Inventory and Clients were
  // highlighted. NavLink matches by path prefix, so /applications considered
  // itself active on every /applications/* URL.
  it("does not light a parent path when a sibling is open", () => {
    renderSidebar({ activePage: "clients", path: "/applications/clients" });
    expect(activeLinks()).toEqual(["Clients"]);
  });

  it("lights nothing spurious on a nested workloads URL", () => {
    renderSidebar({ activePage: "namespaces", path: "/workloads/prod-eu/payments" });
    expect(activeLinks()).toEqual(["Workloads"]);
  });

  it("lights the parent for a drill-down, since it has no entry of its own", () => {
    renderSidebar({ activePage: "inventory", path: "/applications/42" });
    expect(activeLinks()).toEqual(["Inventory"]);
  });

  it("lights the dashboard at the root without matching everything", () => {
    renderSidebar({ activePage: "dashboard", path: "/" });
    expect(activeLinks()).toEqual(["Dashboard"]);
  });
});

describe("entries are real links", () => {
  it("gives every destination an href so it can be opened in a new tab", () => {
    renderSidebar({ activePage: "dashboard" });
    fireEvent.click(groupButton("Administration"));

    const links = within(groupPanel("Administration")).getAllByRole("link");
    expect(links.length).toBeGreaterThan(0);
    links.forEach((link) => expect(link).toHaveAttribute("href"));
  });

  it("points a known entry at its route", () => {
    renderSidebar({ activePage: "dashboard" });
    fireEvent.click(groupButton("Administration"));
    expect(within(groupPanel("Administration")).getByRole("link", { name: /audit logs/i })).toHaveAttribute(
      "href",
      "/admin/audit"
    );
  });

  // A partial icon set fell back to a hollow circle, which reads as an
  // unselected radio button — "choose one" rather than "go here".
  it("renders no per-item glyphs", () => {
    renderSidebar({ activePage: "dashboard" });
    fireEvent.click(groupButton("Administration"));

    within(groupPanel("Administration"))
      .getAllByRole("link")
      .forEach((link) => {
        expect(link.querySelector("svg")).toBeNull();
      });
  });

  it("reports navigation so the mobile drawer can close", () => {
    const onNavigated = vi.fn();
    renderSidebar({ activePage: "dashboard", onNavigated });
    fireEvent.click(groupButton("Administration"));
    fireEvent.click(within(groupPanel("Administration")).getByRole("link", { name: /audit logs/i }));
    expect(onNavigated).toHaveBeenCalledWith("auditLogs");
  });
});

describe("permission filtering", () => {
  it("renders only groups the user has pages in", () => {
    const auditOnly = {
      isAdmin: false,
      role: "user",
      permissions: ["audit:view"],
      accessRules: [],
      clusterAccess: [],
    };
    render(
      <MemoryRouter>
        <Sidebar pages={getVisiblePages(auditOnly)} activePage="auditLogs" open />
      </MemoryRouter>
    );
    expect(screen.getByRole("button", { name: "Administration" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Operate" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Home" })).toBeNull();
  });
});
