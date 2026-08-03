import { describe, expect, it } from "vitest";
import { breadcrumbsFor, labelForPageKey } from "./breadcrumbs.js";
import { ROUTES } from "./routeTable.js";

describe("breadcrumbsFor", () => {
  it("puts the nav group first as a non-link", () => {
    const trail = breadcrumbsFor("auditLogs");
    expect(trail[0]).toMatchObject({ label: "Administration", href: null, isGroup: true });
  });

  it("ends on the current page, unlinked", () => {
    const trail = breadcrumbsFor("auditLogs");
    const last = trail[trail.length - 1];
    expect(last).toMatchObject({ label: "Audit Logs", href: null, isCurrent: true });
  });

  it("links the parent of a drill-down", () => {
    const trail = breadcrumbsFor("applicationDetails", {
      params: { applicationId: "42" },
      currentLabel: "payments-api",
    });
    expect(trail.map((c) => c.label)).toEqual(["Applications", "Inventory", "payments-api"]);
    expect(trail[1].href).toBe("/applications");
    expect(trail[2].href).toBeNull();
  });

  it("uses the caller's label for the current page when given", () => {
    const trail = breadcrumbsFor("clusterOverview", {
      params: { clusterId: "prod-eu" },
      currentLabel: "prod-eu",
    });
    expect(trail[trail.length - 1].label).toBe("prod-eu");
    expect(trail.some((c) => c.href === "/fleet/clusters")).toBe(true);
  });

  it("falls back to a generic label when the caller gives none", () => {
    const trail = breadcrumbsFor("clusterOverview", { params: { clusterId: "prod-eu" } });
    expect(trail[trail.length - 1].label).toBe("Cluster");
  });

  // A crumb that looks like a link and is not is worse than one crumb fewer.
  it("drops an ancestor whose href cannot be built", () => {
    const trail = breadcrumbsFor("resources", {});
    expect(trail.every((crumb) => crumb.href === null || crumb.href.startsWith("/"))).toBe(true);
    expect(trail.some((crumb) => String(crumb.href).includes(":"))).toBe(false);
  });

  it("builds a parent href from the current params", () => {
    const trail = breadcrumbsFor("resources", {
      params: { clusterId: "prod-eu", namespace: "payments" },
    });
    expect(trail.map((c) => c.label)).toEqual(["Operate", "Workloads", "Resources"]);
    expect(trail[1].href).toBe("/workloads");
  });

  it("returns nothing for an unknown page", () => {
    expect(breadcrumbsFor("noSuchPage")).toEqual([]);
  });

  it("never emits an empty label for any route", () => {
    ROUTES.forEach((route) => {
      breadcrumbsFor(route.pageKey, { params: { clusterId: "c", namespace: "n", applicationId: "1" } })
        .forEach((crumb) => {
          expect(String(crumb.label).trim(), route.pageKey).not.toBe("");
        });
    });
  });
});

describe("labelForPageKey", () => {
  it("uses the nav label where there is one", () => {
    expect(labelForPageKey("auditLogs")).toBe("Audit Logs");
    expect(labelForPageKey("namespaces")).toBe("Workloads");
  });

  it("names drill-downs, which have no nav entry", () => {
    expect(labelForPageKey("applicationDetails")).toBe("Application");
    expect(labelForPageKey("clusterOverview")).toBe("Cluster");
  });
});
