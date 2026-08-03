import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * Two navigation rules that are stated in CSS and therefore invisible to
 * component tests, but are product decisions rather than taste.
 *
 * Asserting on stylesheet text is unusual and worth justifying: both of these
 * regressed once already — the underline the moment nav entries became <a>, and
 * the red the moment the active state reused the brand accent — and neither
 * shows up in a render test or a build.
 */

const read = (relative) =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");

const indexCss = read("../index.css");
const sidebarCss = read("./sidebar-flyout.css");
const polishCss = read("./ui-polish.css");

/** The declaration block for a selector, or "" if absent. */
function block(css, selector) {
  const index = css.indexOf(`${selector} {`);
  if (index === -1) {
    return "";
  }
  return css.slice(index, css.indexOf("}", index));
}

describe("nav entries are links that do not look like prose", () => {
  it("clears the default underline on .nav-link", () => {
    expect(block(indexCss, ".nav-link")).toMatch(/text-decoration:\s*none/);
  });
});

describe("red stays reserved for things being wrong", () => {
  // Red means degraded, failed or firing across this product, and the attention
  // feed depends on that scarcity. The current nav item is on screen
  // permanently and signals nothing wrong, so spending red on it devalues the
  // colour everywhere it does mean something.
  const RED = /#(ff|d7|e3|e6)[0-9a-f]{4}\b|rgba?\(\s*(255|230|227)\s*,\s*(41|59|27|46)/i;

  it("defines the selected-item background without red", () => {
    const activeBackgrounds = [...indexCss.matchAll(/--sidebar-nav-active-bg:\s*([^;]+);/g)].map(
      (match) => match[1].trim()
    );
    expect(activeBackgrounds.length).toBeGreaterThan(0);
    activeBackgrounds.forEach((value) => expect(value).not.toMatch(RED));
  });

  it("defines the selected-item text colour without red", () => {
    const activeText = [...indexCss.matchAll(/--sidebar-nav-active-text:\s*([^;]+);/g)].map(
      (match) => match[1].trim()
    );
    expect(activeText.length).toBeGreaterThan(0);
    activeText.forEach((value) => expect(value).not.toMatch(RED));
  });

  it("no longer defines a red selection border at all", () => {
    expect(indexCss).not.toMatch(/--sidebar-nav-active-border/);
    expect(sidebarCss).not.toMatch(/--sidebar-nav-active-border/);
  });

  it("does not tint the active nav entry with the brand accent", () => {
    expect(block(indexCss, ".nav-link.active")).not.toMatch(/var\(--accent/);
    expect(block(polishCss, ".nav-link.active")).not.toMatch(/var\(--accent/);
  });

  it("distinguishes selection by weight as well as tint", () => {
    expect(block(indexCss, ".nav-link.active")).toMatch(/font-weight/);
  });
});
