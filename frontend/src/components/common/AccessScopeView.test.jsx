// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import AccessScopeView from "./AccessScopeView.jsx";

/**
 * The precedence rule every page now depends on.
 *
 * It was re-derived per page, and the recurring failure was always the same: an
 * empty state rendering before the first fetch resolved, so "there is nothing
 * here" appeared when the honest answer was "we do not know yet".
 */

afterEach(cleanup);

const content = <p>CONTENT</p>;
const has = (text) => Boolean(screen.queryByText(text));

describe("loading wins", () => {
  it("shows loading, not empty, before the first fetch resolves", () => {
    render(
      <AccessScopeView pageLoading empty emptyMessage="Nothing here">
        {content}
      </AccessScopeView>
    );
    expect(has("Nothing here")).toBe(false);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("shows loading, not an error, while a retry is in flight", () => {
    render(
      <AccessScopeView pageLoading accessError="Request failed (500)">
        {content}
      </AccessScopeView>
    );
    expect(has("Request failed (500)")).toBe(false);
  });

  it.each([
    ["authLoading", { authLoading: true }],
    ["coreLoading", { coreLoading: true }],
    ["namespacesLoading", { namespacesLoading: true }],
    ["resourcesLoading", { resourcesLoading: true }],
  ])("treats %s as loading too", (_label, props) => {
    render(
      <AccessScopeView {...props} empty emptyMessage="Nothing here">
        {content}
      </AccessScopeView>
    );
    expect(has("Nothing here")).toBe(false);
    expect(has("CONTENT")).toBe(false);
  });
});

describe("denial outranks a generic error", () => {
  it("renders the access-denied state for a 403-shaped message", () => {
    render(<AccessScopeView accessError="Forbidden">{content}</AccessScopeView>);
    expect(screen.getByRole("heading", { name: /access restricted/i })).toBeInTheDocument();
    expect(has("CONTENT")).toBe(false);
  });

  it("renders an ordinary failure as an error, not a denial", () => {
    render(<AccessScopeView accessError="Request failed (500)">{content}</AccessScopeView>);
    expect(screen.queryByRole("heading", { name: /access restricted/i })).toBeNull();
    expect(has("Request failed (500)")).toBe(true);
  });

  it("can be forced into the denied state without an error message", () => {
    render(<AccessScopeView forceAccessDenied>{content}</AccessScopeView>);
    expect(screen.getByRole("heading", { name: /access restricted/i })).toBeInTheDocument();
  });
});

describe("degraded shows the content and says so", () => {
  // Hiding usable-but-old data behind a banner is as unhelpful as showing it as
  // if it were fresh, so this is the one state that renders both.
  it("renders content alongside the warning", () => {
    render(
      <AccessScopeView degraded degradedMessage="Metrics are 20 minutes old.">
        {content}
      </AccessScopeView>
    );
    expect(has("CONTENT")).toBe(true);
    expect(has("Metrics are 20 minutes old.")).toBe(true);
  });

  it("has a default sentence rather than an empty banner", () => {
    render(<AccessScopeView degraded>{content}</AccessScopeView>);
    expect(has("CONTENT")).toBe(true);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("stays quiet when nothing is degraded", () => {
    render(<AccessScopeView>{content}</AccessScopeView>);
    expect(has("CONTENT")).toBe(true);
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("the header stays put", () => {
  // Pages keep their heading through every state, so the screen never goes
  // completely blank while loading or denied.
  it.each([
    ["loading", { pageLoading: true }],
    ["denied", { accessError: "Forbidden" }],
    ["empty", { empty: true, emptyMessage: "Nothing here" }],
    ["loaded", {}],
  ])("renders in the %s state", (_label, props) => {
    render(
      <AccessScopeView {...props} header={<h1>Audit Logs</h1>}>
        {content}
      </AccessScopeView>
    );
    expect(screen.getByRole("heading", { name: "Audit Logs" })).toBeInTheDocument();
  });
});
