// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Does App render at all, on every route.
 *
 * This exists because the production build cannot answer that question. Vite
 * happily bundles a reference to a variable that does not exist, so twice now a
 * refactor has shipped an undefined identifier that only surfaced as a blank
 * screen and "x is not defined" in the browser — `searchScopeParams` once,
 * `allowedResources` once. Both were caught by a person looking at the app,
 * which is not a test strategy.
 *
 * It asserts almost nothing about behaviour on purpose. Every route mounts,
 * nothing throws, and the error boundary does not appear. That is the cheapest
 * check that separates "compiles" from "runs", and it is the check that was
 * missing.
 */

const listClusters = vi.fn();
const getSettings = vi.fn();
const listAlerts = vi.fn();

vi.mock("./api", () => ({
  listClusters: (...a) => listClusters(...a),
  getSettings: (...a) => getSettings(...a),
  listAlerts: (...a) => listAlerts(...a),
  listNamespacesByCluster: async () => ({ items: [] }),
  listNamespaceMetricsByCluster: async () => ({ items: [] }),
  getClusterOverview: async () => ({}),
  updateSettings: async () => ({}),
  testAlertEmail: async () => ({}),
}));

vi.mock("./api/deploymentRequestsApi.js", () => ({
  listMyDeploymentRequests: async () => ({ items: [] }),
  listDeploymentRequests: async () => ({ items: [] }),
}));

vi.mock("./api/integrationsApi.js", () => ({
  listIntegrations: async () => ({ items: [] }),
  getIntegration: async () => ({}),
  testIntegration: async () => ({}),
  setIntegrationEnabled: async () => ({}),
  listIntegrationActivity: async () => ({ items: [] }),
}));

const ADMIN = {
  id: 1,
  name: "Cluster Admin",
  isAdmin: true,
  hasFullAccess: true,
  role: "admin",
  permissions: [],
  accessRules: [],
};

vi.mock("./context/AuthContext.jsx", async () => {
  const authz = await import("./utils/authz.js");
  const access = authz.createAuthAccess(ADMIN);
  return {
    useAuth: () => ({
      ...access,
      user: ADMIN,
      loading: false,
      isAuthenticated: true,
      needsOnboarding: false,
      logout: () => {},
    }),
    AuthProvider: ({ children }) => children,
  };
});

vi.mock("./context/ChangeBundleContext.jsx", () => ({
  useChangeBundle: () => ({ enabled: false, isOpen: false, itemCount: 0, openDrawer: () => {} }),
  ChangeBundleProvider: ({ children }) => children,
}));

const App = (await import("./App.jsx")).default;
const { ROUTES } = await import("./routes/routeTable.js");

beforeEach(() => {
  vi.clearAllMocks();
  // jsdom implements neither of these, and the shell uses both: matchMedia for
  // the responsive drawer, IntersectionObserver for the settings scrollspy.
  // Absent stubs, every route fails on the environment rather than the code.
  window.matchMedia = window.matchMedia || ((query) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
  window.IntersectionObserver = window.IntersectionObserver || class {
    observe() {}
    disconnect() {}
  };
  listClusters.mockResolvedValue({ items: [{ id: "prod-eu", name: "Production EU" }] });
  getSettings.mockResolvedValue({});
  listAlerts.mockResolvedValue({ items: [], metadata: {} });
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const paths = ROUTES.map((route) => [
  route.pageKey,
  route.path
    .replace(":clusterId", "prod-eu")
    .replace(":namespace", "payments")
    .replace(":applicationId", "42")
    .replace(":provider", "jira"),
]);

describe("every route mounts without throwing", () => {
  it.each(paths)("%s", async (pageKey, path) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    );

    // The shell is present, so App itself rendered rather than being replaced
    // by an error boundary.
    await waitFor(() =>
      expect(screen.getByRole("navigation", { name: /main navigation/i })).toBeInTheDocument()
    );
    expect(screen.queryByText(/Something went wrong/i)).toBeNull();
  });

  it("renders the not-found page for an unknown route", async () => {
    render(
      <MemoryRouter initialEntries={["/no-such-page"]}>
        <App />
      </MemoryRouter>
    );
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: /page not found/i })).toBeInTheDocument()
    );
  });
});

describe("no route logs a React error", () => {
  // An undefined identifier inside a child surfaces here even when the shell
  // still renders, which is the shape both previous crashes took.
  it.each(paths.slice(0, 8))("%s", async (pageKey, path) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    );
    await waitFor(() =>
      expect(screen.getByRole("navigation", { name: /main navigation/i })).toBeInTheDocument()
    );

    const fatal = console.error.mock.calls
      .map((args) => String(args[0]))
      .filter((message) => /is not defined|Cannot read|is not a function/.test(message));
    expect(fatal).toEqual([]);
  });
});
