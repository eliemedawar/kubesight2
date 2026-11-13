import { describe, expect, it } from "vitest";
import { activeSection, sourceSections } from "./sourcesNav.js";

const ALL = { vsphere: true, ssh: true, buildProfiles: true, apps: true };

describe("sourceSections", () => {
  it("lists every section in rail order for a full admin", () => {
    expect(sourceSections({}, ALL).map((s) => s.id)).toEqual([
      "vcenter", "cloud", "addresses", "opentofu", "ssh", "packages", "vm-requests", "app-requests",
    ]);
  });

  it("hides what the user cannot manage", () => {
    expect(sourceSections({}, { ssh: true, apps: true }).map((s) => s.id)).toEqual([
      "ssh", "app-requests",
    ]);
  });

  it("counts records and failures from infra", () => {
    const sections = sourceSections({
      vsphere: [{ lastConnectionStatus: "ok" }, { lastConnectionStatus: "error" }, {}],
      cloudAccounts: [{ lastTestStatus: "failed" }],
      profiles: [{ lastTestStatus: "ok" }],
    }, ALL);
    const byId = Object.fromEntries(sections.map((s) => [s.id, s]));
    expect(byId.vcenter).toMatchObject({ count: 3, failing: 1 });
    expect(byId.cloud).toMatchObject({ count: 1, failing: 1 });
    expect(byId.ssh).toMatchObject({ count: 1, failing: 0 });
    expect(byId.packages).toMatchObject({ count: 0, failing: 0 });
    // Sections whose records load inside their own panel carry no count.
    expect(byId.addresses.count).toBeNull();
  });
});

describe("activeSection", () => {
  const sections = sourceSections({}, { ssh: true, buildProfiles: true });
  it("keeps a visible requested section", () => {
    expect(activeSection(sections, "packages")).toBe("packages");
  });
  it("falls back to the first visible section", () => {
    expect(activeSection(sections, "vcenter")).toBe("ssh");
    expect(activeSection(sections, "")).toBe("ssh");
    expect(activeSection([], "ssh")).toBeNull();
  });
});
