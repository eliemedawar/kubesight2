import { describe, expect, it } from "vitest";
import { NAV_PAGES, getVisiblePages } from "../utils/authz.js";
import { routeForPageKey } from "./routeTable.js";
import { GROUPED_PAGE_KEYS, NAV_GROUPS, buildNavTree, groupIdForPageKey } from "./navigation.js";

const ADMIN = { isAdmin: true, hasFullAccess: true, role: "admin", permissions: [], accessRules: [] };

const withPermissions = (permissions) => ({
  isAdmin: false,
  role: "user",
  permissions,
  accessRules: permissions.map((permissionKey) => ({
    effect: "allow",
    clusterId: "prod-eu",
    permissionKey,
  })),
  clusterAccess: ["prod-eu"],
});

describe("the five groups", () => {
  it("is exactly the five the brief names, in order", () => {
    expect(NAV_GROUPS.map((group) => group.label)).toEqual([
      "Home",
      "Operate",
      "Applications",
      "Changes",
      "Administration",
    ]);
  });

  it("lists no page in two groups", () => {
    const all = NAV_GROUPS.flatMap((group) => group.pageKeys);
    expect(new Set(all).size).toBe(all.length);
  });

  it("routes every page it lists", () => {
    const unrouted = [...GROUPED_PAGE_KEYS].filter((pageKey) => !routeForPageKey(pageKey));
    expect(unrouted).toEqual([]);
  });

  // The invariant that keeps the menu honest: a page an admin can see and that
  // is not explicitly hidden must live in a group, or it is unreachable from
  // the sidebar with nothing saying so.
  it("gives every menu-visible page a group", () => {
    const homeless = getVisiblePages(ADMIN)
      .map((page) => page.key)
      .filter((pageKey) => !GROUPED_PAGE_KEYS.has(pageKey));
    expect(homeless).toEqual([]);
  });

  // ...and the converse: a group must not advertise a page nobody can route to.
  it("lists no page that is hidden from the menu", () => {
    const hidden = NAV_PAGES.filter((page) => page.hidden).map((page) => page.key);
    hidden.forEach((pageKey) => {
      expect(GROUPED_PAGE_KEYS.has(pageKey), `${pageKey} is hidden but grouped`).toBe(false);
    });
  });
});

describe("group highlighting", () => {
  it("maps a page to its group", () => {
    expect(groupIdForPageKey("dashboard")).toBe("home");
    expect(groupIdForPageKey("alerts")).toBe("operate");
    expect(groupIdForPageKey("inventory")).toBe("applications");
    expect(groupIdForPageKey("changeBundles")).toBe("changes");
    expect(groupIdForPageKey("auditLogs")).toBe("administration");
  });

  it("has no group for a drill-down, which highlights via its parent instead", () => {
    expect(groupIdForPageKey("applicationDetails")).toBeNull();
    expect(groupIdForPageKey("clusterOverview")).toBeNull();
  });

  // Related work landed in one place: the upgrade centre sat under "Operations"
  // while the clusters it upgrades sat under "Infrastructure".
  it("puts upgrades with the clusters they act on", () => {
    expect(groupIdForPageKey("upgrade")).toBe(groupIdForPageKey("clusters"));
  });
});

describe("buildNavTree", () => {
  it("gives an admin every group with hrefs", () => {
    const tree = buildNavTree(getVisiblePages(ADMIN));
    expect(tree.map((group) => group.id)).toEqual([
      "home",
      "operate",
      "applications",
      "changes",
      "administration",
    ]);
    tree.forEach((group) => {
      group.items.forEach((item) => {
        expect(item.href, item.pageKey).toMatch(/^\//);
        expect(item.label, item.pageKey).toBeTruthy();
      });
    });
  });

  it("preserves the order declared in the group, not NAV_PAGES order", () => {
    const operate = buildNavTree(getVisiblePages(ADMIN)).find((g) => g.id === "operate");
    expect(operate.items.map((i) => i.pageKey)).toEqual(
      NAV_GROUPS.find((g) => g.id === "operate").pageKeys
    );
  });

  // Permission filtering stays: this is getVisiblePages' output, and the tree
  // adds grouping without making any access decision of its own.
  it("drops pages the user may not see", () => {
    const tree = buildNavTree(getVisiblePages(withPermissions(["audit:view"])));
    const keys = tree.flatMap((group) => group.items.map((item) => item.pageKey));
    expect(keys).toContain("auditLogs");
    expect(keys).not.toContain("userManagement");
    expect(keys).not.toContain("settings");
  });

  // A heading with nothing under it reads as something being broken.
  it("drops groups that end up empty", () => {
    const tree = buildNavTree(getVisiblePages(withPermissions(["audit:view"])));
    expect(tree.map((group) => group.id)).toEqual(["administration"]);
    tree.forEach((group) => expect(group.items.length).toBeGreaterThan(0));
  });

  it("returns nothing for a user with no visible pages", () => {
    expect(buildNavTree([])).toEqual([]);
  });

  it("keeps hidden pages out of the menu while leaving them routable", () => {
    const keys = buildNavTree(getVisiblePages(ADMIN)).flatMap((g) =>
      g.items.map((i) => i.pageKey)
    );
    expect(keys).not.toContain("imageRegistries");
    expect(keys).not.toContain("resources");
    // Still addressable — that is the point of hiding rather than deleting.
    expect(routeForPageKey("imageRegistries")).toBeTruthy();
    expect(routeForPageKey("resources")).toBeTruthy();
  });
});
