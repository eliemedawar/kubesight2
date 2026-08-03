// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { useSearchParams } from "react-router-dom";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

/**
 * The three deep-link handshakes are gone.
 *
 * Each was a one-shot storage write by whoever linked somewhere, consumed on
 * mount by the destination, and each existed only because there was no router
 * to put the value in the address. Their own comments said so. This asserts
 * they are not merely unused but absent — a handshake left in the tree is one a
 * future page can pick back up.
 */

afterEach(cleanup);

const SOURCES = import.meta.glob("../**/*.{js,jsx}", { eager: true, query: "?raw", import: "default" });

const appSources = () =>
  Object.entries(SOURCES).filter(
    ([path]) => !path.includes(".test.") && !path.includes("deepLinks")
  );

describe("no storage-based deep links remain", () => {
  it.each([
    ["consumeAlertsTabHint", "the Alerts tab"],
    ["setAlertsTabHint", "the Alerts tab"],
    ["consumeSettingsSectionHint", "the Settings section"],
    ["setSettingsSectionHint", "the Settings section"],
    ["kubesight.ticketing.provider", "the Ticketing provider"],
  ])("%s is gone (%s is a URL now)", (symbol) => {
    const offenders = appSources()
      .filter(([, source]) => typeof source === "string" && source.includes(symbol))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });

  // The Ticketing key was the one that actually leaked: unlike every other
  // per-user key in this app it was not namespaced by user id, so it survived a
  // sign-out on a shared machine.
  it("stores no navigation state in sessionStorage", () => {
    const offenders = appSources()
      .filter(([, source]) => typeof source === "string" && /sessionStorage\.(get|set)Item/.test(source))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});

/** A minimal stand-in for the pattern all three now use. */
function TabProbe() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "open";
  return (
    <div>
      <span data-testid="tab">{tab}</span>
      <button type="button" onClick={() => setParams({ tab: "policies" }, { replace: true })}>
        policies
      </button>
    </div>
  );
}

describe("the replacement is addressable", () => {
  it("opens on the tab named in the URL", () => {
    render(
      <MemoryRouter initialEntries={["/alerts?tab=policies"]}>
        <Routes>
          <Route path="/alerts" element={<TabProbe />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByTestId("tab")).toHaveTextContent("policies");
  });

  it("falls back when the URL names nothing", () => {
    render(
      <MemoryRouter initialEntries={["/alerts"]}>
        <Routes>
          <Route path="/alerts" element={<TabProbe />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByTestId("tab")).toHaveTextContent("open");
  });
});
