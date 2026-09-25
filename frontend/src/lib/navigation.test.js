import { describe, expect, it } from "vitest";
import { NAV_PAGES } from "../utils/authz.js";
import { routeForPageKey } from "../routes/routeTable.js";
import {
  FOLDED_PAGES,
  NAV_GROUPS,
  WORKSPACES,
  buildJumpTargets,
  buildNavGroups,
  sidebarKeyFor,
  workspaceOfPage,
} from "./navigation.js";

const sidebarPages = NAV_PAGES.filter((page) => !page.hidden);

describe("navigation layout", () => {
  it("places every sidebar page exactly once", () => {
    const placed = [
      ...NAV_GROUPS.flatMap((group) => group.items.filter((item) => item.page).map((item) => item.page)),
      ...Object.values(WORKSPACES).flatMap((ws) => ws.tabs.map((tab) => tab.pageKey)),
      ...Object.keys(FOLDED_PAGES),
    ];
    expect(new Set(placed).size).toBe(placed.length);
    expect(placed.sort()).toEqual(sidebarPages.map((page) => page.key).sort());
  });

  it("gives every placed page a route", () => {
    sidebarPages.forEach((page) => {
      expect(routeForPageKey(page.key), page.key).not.toBeNull();
    });
  });

  it("no longer offers the cluster topology page", () => {
    expect(NAV_PAGES.some((page) => page.key === "topology")).toBe(false);
    expect(routeForPageKey("topology")).toBeNull();
  });

  it("folds CI, application intelligence and mobile apps into the Build Center", () => {
    ["serviceCatalog", "applicationIntelligence", "mobileApps"].forEach((key) => {
      expect(workspaceOfPage(key)).toBe("buildCenter");
    });
    // Application Intelligence is a tab of each CI service, not of the workspace.
    const build = buildNavGroups(sidebarPages)
      .flatMap((group) => group.items)
      .find((item) => item.key === "buildCenter");
    expect(build.tabs.map((tab) => tab.pageKey)).toEqual(["serviceCatalog", "mobileApps"]);
    expect(
      buildNavGroups(sidebarPages).some((group) => group.id === "more")
    ).toBe(false);
    ["blueprints", "applicationServices", "clients"].forEach((key) => {
      expect(workspaceOfPage(key)).toBe("architecture");
    });
    expect(sidebarKeyFor("mobileApps")).toBe("buildCenter");
    expect(sidebarKeyFor("alerts")).toBe("alerts");
  });

  it("drops what the user cannot see and opens a workspace on its first allowed tab", () => {
    const visible = sidebarPages.filter((page) =>
      ["dashboard", "mobileApps", "clients"].includes(page.key)
    );
    const groups = buildNavGroups(visible);
    const items = groups.flatMap((group) => group.items);
    expect(items.map((item) => item.key)).toEqual(["dashboard", "buildCenter", "architecture"]);
    const build = items.find((item) => item.key === "buildCenter");
    expect(build.pageKey).toBe("mobileApps");
    expect(build.tabs.map((tab) => tab.pageKey)).toEqual(["mobileApps"]);
  });

  it("keeps a page the layout forgot instead of hiding it", () => {
    const groups = buildNavGroups([{ key: "somethingNew", label: "Something new" }]);
    expect(groups).toEqual([
      {
        id: "more",
        label: "More",
        items: [{ key: "somethingNew", label: "Something new", pageKey: "somethingNew" }],
      },
    ]);
  });

  it("lists workspace tabs individually in the jump palette", () => {
    const targets = buildJumpTargets(buildNavGroups(sidebarPages));
    const mobile = targets.find((target) => target.pageKey === "mobileApps");
    expect(mobile.context).toBe("Applications › Build Center");
    expect(targets.find((target) => target.pageKey === "buildCenter")).toBeUndefined();
  });
});
