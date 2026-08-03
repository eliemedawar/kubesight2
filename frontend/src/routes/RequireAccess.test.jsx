// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import NotFoundPage from "../pages/NotFoundPage.jsx";
import { ROUTES } from "./routeTable.js";
import RequireAccess, { accessDeniedMessage } from "./RequireAccess.jsx";

/**
 * The behaviour change recorded as F2 in ROUTING-AUDIT.md: a page the user may
 * not see is answered at its own URL instead of silently redirecting. These
 * tests exist because "the URL does not change" is the whole point and is the
 * easy thing to regress later.
 */

afterEach(cleanup);

const allowAll = () => true;
const denyAll = () => false;
const allowOnly = (...keys) => (pageKey) => keys.includes(pageKey);

function renderApp({ path, isPageAllowed }) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {ROUTES.map((route) => (
          <Route
            key={route.pageKey}
            path={route.path}
            element={
              <RequireAccess pageKey={route.pageKey} isPageAllowed={isPageAllowed}>
                <div data-testid="page">{route.pageKey}</div>
              </RequireAccess>
            }
          />
        ))}
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("a page the user may see", () => {
  it("renders normally", () => {
    renderApp({ path: "/admin/users", isPageAllowed: allowAll });
    expect(screen.getByTestId("page")).toHaveTextContent("userManagement");
  });
});

describe("a page the user may not see", () => {
  it("renders the denial instead of the page", () => {
    renderApp({ path: "/admin/users", isPageAllowed: denyAll });
    expect(screen.queryByTestId("page")).toBeNull();
    expect(screen.getByRole("heading", { name: /access restricted/i })).toBeInTheDocument();
  });

  it("names the page so the operator knows what to ask for", () => {
    renderApp({ path: "/admin/users", isPageAllowed: denyAll });
    expect(screen.getByText(/User Management/)).toBeInTheDocument();
  });

  // The regression this whole change exists to prevent: a pasted link that is
  // correct, opened by someone who lacks the permission, must not look like a
  // broken link. It previously landed on the dashboard with no explanation.
  it("does not silently render some other page", () => {
    renderApp({ path: "/admin/users", isPageAllowed: allowOnly("dashboard") });
    expect(screen.queryByText("dashboard")).toBeNull();
    expect(screen.queryByTestId("page")).toBeNull();
  });

  it("denies drill-down routes on the same rule as their parent", () => {
    renderApp({ path: "/applications/42", isPageAllowed: denyAll });
    expect(screen.queryByTestId("page")).toBeNull();
    expect(screen.getByRole("heading", { name: /access restricted/i })).toBeInTheDocument();
  });
});

describe("denial is distinguished from not-found", () => {
  it("treats an unknown URL as not-found even when everything is permitted", () => {
    renderApp({ path: "/nope", isPageAllowed: allowAll });
    expect(screen.getByRole("heading", { name: /page not found/i })).toBeInTheDocument();
  });

  it("treats a known URL as denied rather than not-found", () => {
    renderApp({ path: "/admin/audit", isPageAllowed: denyAll });
    expect(screen.queryByRole("heading", { name: /page not found/i })).toBeNull();
    expect(screen.getByRole("heading", { name: /access restricted/i })).toBeInTheDocument();
  });
});

describe("accessDeniedMessage", () => {
  it("names a known page", () => {
    expect(accessDeniedMessage("auditLogs")).toMatch(/Audit Logs/);
  });

  // Drill-downs are not NAV_PAGES entries, so they have no label to quote.
  it("falls back to a generic message for a page with no nav label", () => {
    expect(accessDeniedMessage("applicationDetails")).toMatch(/this page/);
    expect(accessDeniedMessage("clusterOverview")).toMatch(/this page/);
  });

  it("never renders an empty or undefined page name", () => {
    ROUTES.forEach((route) => {
      const message = accessDeniedMessage(route.pageKey);
      expect(message, route.pageKey).not.toMatch(/undefined|null|\s{2,}/);
    });
  });
});
