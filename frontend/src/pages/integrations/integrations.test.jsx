// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listIntegrations = vi.fn();
const getIntegration = vi.fn();
const testIntegration = vi.fn();
const setIntegrationEnabled = vi.fn();
const listIntegrationActivity = vi.fn();

vi.mock("../../api/integrationsApi.js", () => ({
  listIntegrations: (...args) => listIntegrations(...args),
  getIntegration: (...args) => getIntegration(...args),
  testIntegration: (...args) => testIntegration(...args),
  setIntegrationEnabled: (...args) => setIntegrationEnabled(...args),
  listIntegrationActivity: (...args) => listIntegrationActivity(...args),
}));

const IntegrationsHubPage = (await import("./IntegrationsHubPage.jsx")).default;
const IntegrationDetailPage = (await import("./IntegrationDetailPage.jsx")).default;

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  listIntegrationActivity.mockResolvedValue({ items: [] });
});

/** Exactly the descriptor shape contract 2 pins, including no `configured` field. */
const jira = (overrides = {}) => ({
  key: "jira",
  name: "Jira",
  category: "Ticketing",
  status: "connected",
  enabled: true,
  lastTestedAt: "2026-08-02T10:14:00Z",
  lastSuccessfulSyncAt: "2026-08-02T10:00:00Z",
  message: "Connection healthy",
  capabilities: ["ticket-sync", "deployment-approval"],
  usedBy: ["Deployment requests"],
  actions: ["configure", "test", "disable"],
  ...overrides,
});

const renderHub = () =>
  render(
    <MemoryRouter initialEntries={["/integrations"]}>
      <Routes>
        <Route path="/integrations" element={<IntegrationsHubPage />} />
      </Routes>
    </MemoryRouter>
  );

const renderDetail = (tab = "overview", path = "/integrations/jira") =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/integrations/:provider" element={<IntegrationDetailPage tab={tab} />} />
        <Route
          path="/integrations/:provider/activity"
          element={<IntegrationDetailPage tab="activity" />}
        />
        <Route
          path="/integrations/:provider/used-by"
          element={<IntegrationDetailPage tab="usedBy" />}
        />
      </Routes>
    </MemoryRouter>
  );

describe("the hub renders what the backend sends", () => {
  it("hardcodes no provider list", async () => {
    // A provider nothing in this codebase has heard of must render normally.
    listIntegrations.mockResolvedValue({
      items: [jira({ key: "brand-new", name: "Brand New Thing", category: "Nonesuch" })],
    });
    renderHub();
    expect(await screen.findByText("Brand New Thing")).toBeInTheDocument();
    expect(screen.getByText("Nonesuch")).toBeInTheDocument();
  });

  it("reads the list from data.items, not a bare array", async () => {
    listIntegrations.mockResolvedValue({ items: [jira()] });
    renderHub();
    expect(await screen.findByText("Jira")).toBeInTheDocument();
  });

  it("shows only what this user may see, without deciding that itself", async () => {
    // The backend filters per user; one card is a complete hub for that user.
    listIntegrations.mockResolvedValue({
      items: [jira({ key: "registries", name: "Container registries", category: "Artifacts" })],
    });
    renderHub();
    expect(await screen.findByText("Container registries")).toBeInTheDocument();
    expect(screen.queryByText("Jira")).toBeNull();
  });

  it("leads the summary with what is wrong", async () => {
    listIntegrations.mockResolvedValue({
      items: [
        jira(),
        jira({ key: "zoho", name: "Zoho", status: "degraded" }),
      ],
    });
    renderHub();
    const summary = await screen.findByText(/degraded/);
    expect(summary.textContent.indexOf("degraded")).toBeLessThan(
      summary.textContent.indexOf("connected")
    );
  });

  it("says so when the account has no integrations at all", async () => {
    listIntegrations.mockResolvedValue({ items: [] });
    renderHub();
    expect(await screen.findByText(/No integrations are available/)).toBeInTheDocument();
  });
});

describe("actions come from the actions array", () => {
  it("renders exactly the actions the backend offered", async () => {
    getIntegration.mockResolvedValue(jira({ actions: ["configure", "test", "disable"] }));
    renderDetail();
    expect(await screen.findByRole("button", { name: /Test connection/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Disable$/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Enable$/ })).toBeNull();
  });

  // The rule that matters: a viewer may read someone else's test result but may
  // not provoke one. An empty actions array must produce no controls at all.
  it("offers nothing when the array is empty", async () => {
    getIntegration.mockResolvedValue(jira({ actions: [] }));
    renderDetail();
    await screen.findByText("Connection healthy");
    expect(screen.queryByRole("button", { name: /Test connection/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Disable$/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Enable$/ })).toBeNull();
  });

  it("offers enable, not disable, when that is what was sent", async () => {
    getIntegration.mockResolvedValue(
      jira({ status: "disabled", enabled: false, actions: ["configure", "enable"] })
    );
    renderDetail();
    expect(await screen.findByRole("button", { name: /^Enable$/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Disable$/ })).toBeNull();
  });

  // SMTP has no on/off switch of its own; contract 2 strips those actions
  // rather than rendering a control that would do nothing.
  it("renders no toggle for a provider that has none", async () => {
    getIntegration.mockResolvedValue(
      jira({ key: "smtp", name: "SMTP", actions: ["configure", "test"] })
    );
    renderDetail();
    await screen.findByRole("button", { name: /Test connection/ });
    expect(screen.queryByRole("button", { name: /^Enable$/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Disable$/ })).toBeNull();
  });
});

describe("describing never tests", () => {
  it("does not test while loading the hub", async () => {
    listIntegrations.mockResolvedValue({ items: [jira()] });
    renderHub();
    await screen.findByText("Jira");
    expect(testIntegration).not.toHaveBeenCalled();
  });

  it("does not test while opening a detail screen", async () => {
    getIntegration.mockResolvedValue(jira());
    renderDetail();
    await screen.findByText("Connection healthy");
    expect(testIntegration).not.toHaveBeenCalled();
  });
});

describe("404 and 403 are different answers", () => {
  it("says the provider does not exist on a 404", async () => {
    const err = new Error("Unknown integration.");
    err.status = 404;
    getIntegration.mockRejectedValue(err);
    renderDetail();
    expect(await screen.findByText(/no integration called/i)).toBeInTheDocument();
  });

  it("says access is restricted on a 403", async () => {
    const err = new Error("You do not have access to this resource.");
    err.status = 403;
    getIntegration.mockRejectedValue(err);
    renderDetail();
    expect(await screen.findByRole("heading", { name: /Access restricted/ })).toBeInTheDocument();
    expect(screen.queryByText(/no integration called/i)).toBeNull();
  });
});

describe("a broken provider degrades to a card", () => {
  // Contract 2: one failing adapter returns an "unavailable" descriptor rather
  // than blanking the hub for everyone else.
  it("still renders the rest of the hub", async () => {
    listIntegrations.mockResolvedValue({
      items: [
        jira(),
        jira({
          key: "hermes",
          name: "Hermes",
          category: "Intelligence",
          status: "not_configured",
          message: "Status unavailable: adapter raised",
          actions: [],
          capabilities: [],
          usedBy: [],
          lastTestedAt: null,
          lastSuccessfulSyncAt: null,
        }),
      ],
    });
    renderHub();
    expect(await screen.findByText("Jira")).toBeInTheDocument();
    expect(screen.getByText("Hermes")).toBeInTheDocument();
    expect(screen.getByText(/Status unavailable/)).toBeInTheDocument();
  });
});

describe("timestamps", () => {
  it("renders Never rather than blank when a provider has no sync concept", async () => {
    getIntegration.mockResolvedValue(jira({ lastSuccessfulSyncAt: null }));
    renderDetail();
    await screen.findByText("Connection healthy");
    expect(screen.getAllByText("Never").length).toBeGreaterThan(0);
  });
});

describe("tabs", () => {
  it("links each tab to its own address", async () => {
    getIntegration.mockResolvedValue(jira());
    renderDetail();
    const tabs = await screen.findByRole("navigation", { name: /Integration sections/ });
    expect(within(tabs).getByRole("link", { name: "Overview" })).toHaveAttribute(
      "href",
      "/integrations/jira"
    );
    expect(within(tabs).getByRole("link", { name: "Activity" })).toHaveAttribute(
      "href",
      "/integrations/jira/activity"
    );
    expect(within(tabs).getByRole("link", { name: "Used by" })).toHaveAttribute(
      "href",
      "/integrations/jira/used-by"
    );
  });

  it("lists what depends on the integration", async () => {
    getIntegration.mockResolvedValue(jira());
    renderDetail("usedBy", "/integrations/jira/used-by");
    expect(await screen.findByText("Deployment requests")).toBeInTheDocument();
  });

  it("renders activity from data.items", async () => {
    getIntegration.mockResolvedValue(jira());
    listIntegrationActivity.mockResolvedValue({
      items: [
        { id: "1", at: "2026-08-02T09:00:00Z", outcome: "ok", summary: "Ticket ABC-1 received", detail: "" },
        { id: "2", at: null, outcome: "error", summary: "Sync failed", detail: "timeout" },
      ],
    });
    renderDetail("activity", "/integrations/jira/activity");
    expect(await screen.findByText("Ticket ABC-1 received")).toBeInTheDocument();
    // An entry with no timestamp still renders; dropping it would turn a
    // partial history into a misleading one.
    expect(screen.getByText("Sync failed")).toBeInTheDocument();
    expect(screen.getByText("Time not recorded")).toBeInTheDocument();
  });

  it("says the timeline is empty rather than showing nothing", async () => {
    getIntegration.mockResolvedValue(jira());
    listIntegrationActivity.mockResolvedValue({ items: [] });
    renderDetail("activity", "/integrations/jira/activity");
    expect(await screen.findByText(/No activity recorded/)).toBeInTheDocument();
  });
});

describe("testing", () => {
  it("shows a pending state rather than a deadline", async () => {
    getIntegration.mockResolvedValue(jira());
    let resolveTest;
    testIntegration.mockReturnValue(new Promise((resolve) => { resolveTest = resolve; }));
    renderDetail();

    const button = await screen.findByRole("button", { name: /Test connection/ });
    button.click();

    await waitFor(() => expect(screen.getByText(/can take a moment/)).toBeInTheDocument());
    resolveTest({ ok: true, message: "Connection succeeded." });
    await waitFor(() => expect(screen.getByText("Connection succeeded.")).toBeInTheDocument());
  });

  it("re-reads the descriptor after a test rather than guessing the new status", async () => {
    getIntegration.mockResolvedValue(jira());
    testIntegration.mockResolvedValue({ ok: true, message: "ok" });
    renderDetail();

    const button = await screen.findByRole("button", { name: /Test connection/ });
    expect(getIntegration).toHaveBeenCalledTimes(1);
    button.click();
    await waitFor(() => expect(getIntegration).toHaveBeenCalledTimes(2));
  });
});
