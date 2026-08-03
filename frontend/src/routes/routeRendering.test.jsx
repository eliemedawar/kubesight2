// @vitest-environment jsdom
//
// The suite runs in the node environment by default (it is almost all pure
// functions, and node is faster). Component tests opt into jsdom per file with
// the docblock above, and register the DOM matchers explicitly below, so the
// existing tests keep their environment and no global setup file is needed.
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import NotFoundPage from "../pages/NotFoundPage.jsx";
import { ROUTES } from "./routeTable.js";
import { pageKeyForPath } from "./paths.js";

/**
 * The shell derives the active page with `matchPath`, while `<Routes>` decides
 * independently what to render. Those are two calls into React Router's ranking
 * from different places, and if they ever disagreed the chrome would name one
 * page while the body showed another.
 *
 * routeTable.test.js checks the derivation in isolation. This checks it against
 * the thing it has to agree with: an actual render.
 */

afterEach(cleanup);

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {ROUTES.map((route) => (
          <Route
            key={route.pageKey}
            path={route.path}
            element={<div data-testid="page">{route.pageKey}</div>}
          />
        ))}
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("what renders matches what the shell thinks is open", () => {
  it.each(ROUTES.filter((route) => !route.path.includes(":")).map((route) => route.path))(
    "agrees on %s",
    (path) => {
      renderAt(path);
      expect(screen.getByTestId("page")).toHaveTextContent(pageKeyForPath(path));
    }
  );

  it.each([
    ["/applications/42", "applicationDetails"],
    ["/applications/catalog", "serviceCatalog"],
    ["/applications/intelligence", "applicationIntelligence"],
    ["/fleet/clusters/docker-desktop", "clusterOverview"],
    ["/fleet/connections", "clusterManagement"],
    ["/workloads/prod-eu/kube-system", "resources"],
  ])("agrees on %s -> %s", (path, expected) => {
    renderAt(path);
    expect(screen.getByTestId("page")).toHaveTextContent(expected);
    expect(pageKeyForPath(path)).toBe(expected);
  });
});

describe("unmatched URLs", () => {
  it.each(["/nope", "/admin/nope", "/fleet", "/changes"])(
    "renders the not-found page at %s",
    (path) => {
      renderAt(path);
      expect(screen.queryByTestId("page")).toBeNull();
      expect(screen.getByRole("heading", { name: /page not found/i })).toBeInTheDocument();
    }
  );

  // The regression that motivates NotFoundPage: the old default: arm rendered
  // the dashboard for anything it did not recognise, so a stale link looked
  // like it had worked.
  it("does not fall back to the dashboard", () => {
    renderAt("/nope");
    expect(screen.queryByTestId("page")).toBeNull();
  });
});
