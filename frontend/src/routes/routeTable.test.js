import { describe, expect, it } from "vitest";
import { NAV_PAGES, pageAllowed } from "../utils/authz.js";
import {
  RESERVED_APPLICATION_SEGMENTS,
  ROUTES,
  SCOPE,
  navPageKeyFor,
  routeForPageKey,
  routeHidesBundleFab,
  routeLoadingLabel,
  routeNeedsClusterContext,
  routeNeedsNamespaceContext,
} from "./routeTable.js";
import { matchPath, pageKeyForPath, pathForPageKey, isKnownPath } from "./paths.js";

/**
 * The route table is the thing that makes "removing a page is deleting one
 * entry" true, so the properties worth testing are the ones that break when
 * someone adds a page and updates only half of it.
 */

describe("route table integrity", () => {
  it("has a unique pageKey per route", () => {
    const keys = ROUTES.map((route) => route.pageKey);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("has a unique path per route", () => {
    const paths = ROUTES.map((route) => route.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("gives every route an absolute path", () => {
    ROUTES.forEach((route) => {
      expect(route.path.startsWith("/")).toBe(true);
    });
  });

  it("gives every route a loading label and a valid scope", () => {
    const scopes = new Set(Object.values(SCOPE));
    ROUTES.forEach((route) => {
      expect(route.loading, `${route.pageKey} has no loading label`).toBeTruthy();
      expect(scopes.has(route.scope), `${route.pageKey} scope=${route.scope}`).toBe(true);
    });
  });

  it("points every parent at a route that exists", () => {
    ROUTES.filter((route) => route.parent).forEach((route) => {
      expect(routeForPageKey(route.parent), `${route.pageKey} -> ${route.parent}`).toBeTruthy();
    });
  });

  // The audit's central claim: the sidebar is driven by NAV_PAGES, so a nav
  // entry with no route is an unreachable menu item.
  it("routes every non-hidden nav page", () => {
    const missing = NAV_PAGES.filter((page) => !routeForPageKey(page.key)).map((p) => p.key);
    expect(missing).toEqual([]);
  });

  // ...and the reverse: a route whose pageKey no RBAC rule recognises can never
  // be authorized, so it would render as permanently denied.
  it("gives every route a pageKey the authorization layer knows", () => {
    const permissive = {
      isAdmin: true,
      hasFullAccess: true,
      role: "admin",
      permissions: [],
      accessRules: [],
    };
    const unknown = ROUTES.filter((route) => !pageAllowed(permissive, route.pageKey)).map(
      (route) => route.pageKey
    );
    expect(unknown).toEqual([]);
  });
});

describe("scope drives topbar selectors and the no-clusters banner", () => {
  it("treats namespace scope as implying cluster scope", () => {
    ROUTES.filter((route) => route.scope === SCOPE.NAMESPACE).forEach((route) => {
      expect(routeNeedsNamespaceContext(route.pageKey)).toBe(true);
      expect(routeNeedsClusterContext(route.pageKey)).toBe(true);
    });
  });

  it("asks for no selectors on unscoped routes", () => {
    expect(routeNeedsClusterContext("settings")).toBe(false);
    expect(routeNeedsNamespaceContext("settings")).toBe(false);
    expect(routeNeedsClusterContext("userManagement")).toBe(false);
  });

  it("keeps the cluster selector on the cluster-scoped screens", () => {
    ["dashboard", "clusters", "clusterOverview", "inventory", "alerts", "upgrade"].forEach(
      (pageKey) => {
        expect(routeNeedsClusterContext(pageKey), pageKey).toBe(true);
      }
    );
  });

  // Regression: App.jsx:1812 suppressed the banner with a hardcoded list of
  // four pages that was never updated as pages were added, so unscoped screens
  // like Ticketing and Change Bundles nagged about having no clusters.
  it("does not show the no-clusters banner on screens that take no cluster", () => {
    ["ticketing", "changeBundles", "auditLogs", "clients", "components"].forEach((pageKey) => {
      expect(routeNeedsClusterContext(pageKey), pageKey).toBe(false);
    });
  });
});

describe("nav highlighting", () => {
  it("highlights the parent nav entry for a drill-down", () => {
    expect(navPageKeyFor("applicationDetails")).toBe("inventory");
    expect(navPageKeyFor("clusterOverview")).toBe("clusters");
    expect(navPageKeyFor("resources")).toBe("namespaces");
  });

  it("highlights itself for a top-level page", () => {
    expect(navPageKeyFor("dashboard")).toBe("dashboard");
    expect(navPageKeyFor("settings")).toBe("settings");
  });
});

describe("path <-> pageKey", () => {
  it("round-trips every param-free route", () => {
    ROUTES.filter((route) => !route.path.includes(":")).forEach((route) => {
      const path = pathForPageKey(route.pageKey);
      expect(path, route.pageKey).toBe(route.path);
      expect(pageKeyForPath(path), path).toBe(route.pageKey);
    });
  });

  it("resolves the root path to the dashboard", () => {
    expect(pageKeyForPath("/")).toBe("dashboard");
  });

  it("extracts path params", () => {
    expect(matchPath("/applications/42")).toEqual({
      pageKey: "applicationDetails",
      params: { applicationId: "42" },
    });
    expect(matchPath("/fleet/clusters/docker-desktop")).toEqual({
      pageKey: "clusterOverview",
      params: { clusterId: "docker-desktop" },
    });
    expect(matchPath("/workloads/prod-eu/kube-system")).toEqual({
      pageKey: "resources",
      params: { clusterId: "prod-eu", namespace: "kube-system" },
    });
  });

  it("builds parameterised paths", () => {
    expect(pathForPageKey("applicationDetails", { applicationId: "42" })).toBe(
      "/applications/42"
    );
    expect(pathForPageKey("resources", { clusterId: "prod-eu", namespace: "default" })).toBe(
      "/workloads/prod-eu/default"
    );
  });

  it("throws rather than emitting a literal ':param' when an id is missing", () => {
    expect(() => pathForPageKey("applicationDetails")).toThrow(/applicationDetails/);
  });

  it("returns null for an unknown pageKey", () => {
    expect(pathForPageKey("noSuchPage")).toBeNull();
  });

  // Static segments must outrank the dynamic :applicationId sibling, otherwise
  // /applications/catalog loads the detail page for an app named "catalog".
  it("prefers static application segments over the id param", () => {
    RESERVED_APPLICATION_SEGMENTS.forEach((segment) => {
      const pageKey = pageKeyForPath(`/applications/${segment}`);
      expect(pageKey, segment).not.toBe("applicationDetails");
    });
    expect(pageKeyForPath("/applications/catalog")).toBe("serviceCatalog");
    expect(pageKeyForPath("/applications/intelligence")).toBe("applicationIntelligence");
    expect(pageKeyForPath("/applications/999")).toBe("applicationDetails");
  });

  it("keeps cluster management off the /fleet/clusters/:clusterId path", () => {
    expect(pageKeyForPath("/fleet/connections")).toBe("clusterManagement");
    expect(pageKeyForPath("/fleet/clusters/connections")).toBe("clusterOverview");
  });

  it("reports unknown paths so a 404 can be told from an access denial", () => {
    expect(isKnownPath("/nope")).toBe(false);
    expect(isKnownPath("/admin/nope")).toBe(false);
    expect(pageKeyForPath("/nope")).toBe("");
    expect(isKnownPath("/admin/users")).toBe(true);
  });
});

describe("chrome flags", () => {
  it("hides the change-bundle FAB only where it would cover the page", () => {
    expect(routeHidesBundleFab("resources")).toBe(true);
    expect(routeHidesBundleFab("dashboard")).toBe(false);
  });

  it("falls back to a generic loading label for an unknown page", () => {
    expect(routeLoadingLabel("dashboard")).toBe("Loading dashboard...");
    expect(routeLoadingLabel("noSuchPage")).toBe("Loading page...");
  });
});
