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

function renderSidebar({ activePage = "dashboard", onNavigated } = {}) {
  return render(
    <MemoryRouter>
      <Sidebar pages={PAGES} activePage={activePage} onNavigated={onNavigated} open />
    </MemoryRouter>
  );
}

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

  // Opening one group must not tidy away another the user opened on purpose.
  it("lets several groups stay open at once", () => {
    renderSidebar();
    fireEvent.click(groupButton("Changes"));
    fireEvent.click(groupButton("Applications"));

    expect(isExpanded("Changes")).toBe(true);
    expect(isExpanded("Applications")).toBe(true);
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

  it("expands the group you navigate into", () => {
    const { rerender } = renderSidebar({ activePage: "dashboard" });
    expect(isExpanded("Applications")).toBe(false);

    rerender(
      <MemoryRouter>
        <Sidebar pages={PAGES} activePage="inventory" open />
      </MemoryRouter>
    );
    expect(isExpanded("Applications")).toBe(true);
  });

  // Drill-downs highlight through their parent, so opening an application
  // detail page must not leave the menu looking like nothing is selected.
  it("keeps the parent group open on a drill-down", () => {
    renderSidebar({ activePage: "inventory" });
    expect(isExpanded("Applications")).toBe(true);
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
