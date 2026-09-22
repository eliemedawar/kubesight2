import { describe, expect, it } from "vitest";
import {
  ROUTES,
  navKeyOf,
  pageKeyOf,
  patternSegments,
  routeForPageKey,
  slugifyValue,
} from "./routeTable.js";
import { buildRoute, hashForPage, parseRoute, sameRoute } from "./routeUrl.js";

/** A URL that should resolve to this exact route row, with sample params. */
function sampleFor(route) {
  const params = {};
  patternSegments(route.path).forEach((slot) => {
    if (slot.literal !== undefined) {
      return;
    }
    const descriptor = route.params?.[slot.name];
    if (descriptor?.values) {
      // Deliberately not the default, so the segment is actually emitted.
      params[slot.name] =
        descriptor.values.find((value) => value !== route.defaults?.[slot.name]) ||
        descriptor.values[0];
    } else {
      params[slot.name] = `sample-${slot.name}`;
    }
  });
  const query = {};
  (route.query || []).forEach((key) => {
    query[key] = `q-${key}`;
  });
  return { key: route.key, params, query };
}

describe("route table", () => {
  it("has unique keys", () => {
    const keys = ROUTES.map((route) => route.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("gives every enumerated param a default that is one of its values", () => {
    ROUTES.forEach((route) => {
      Object.entries(route.params || {}).forEach(([name, descriptor]) => {
        if (!descriptor.values) {
          return;
        }
        const fallback = route.defaults?.[name];
        if (fallback !== undefined) {
          expect(descriptor.values, `${route.key}.${name}`).toContain(fallback);
        }
      });
    });
  });

  it("gives every enumerated param distinct slugs", () => {
    ROUTES.forEach((route) => {
      Object.entries(route.params || {}).forEach(([name, descriptor]) => {
        if (!descriptor.values) {
          return;
        }
        const slugs = descriptor.values.map(slugifyValue);
        expect(new Set(slugs).size, `${route.key}.${name}`).toBe(slugs.length);
      });
    });
  });

  it("marks optional params only at the tail of a pattern", () => {
    ROUTES.forEach((route) => {
      const slots = patternSegments(route.path);
      let seenOptional = false;
      slots.forEach((slot) => {
        if (slot.optional) {
          seenOptional = true;
        } else if (seenOptional) {
          throw new Error(`${route.key}: required segment after an optional one`);
        }
      });
    });
  });

  it("resolves every page key to a route row", () => {
    const pageKeys = new Set(ROUTES.map(pageKeyOf));
    pageKeys.forEach((pageKey) => {
      expect(routeForPageKey(pageKey), pageKey).toBeTruthy();
    });
  });

  it("gives every row a nav key", () => {
    ROUTES.forEach((route) => {
      expect(navKeyOf(route), route.key).toBeTruthy();
    });
  });
});

describe("parseRoute / buildRoute round trip", () => {
  it("round-trips every route row without shadowing", () => {
    ROUTES.forEach((route) => {
      const sample = sampleFor(route);
      const hash = buildRoute(sample);
      const parsed = parseRoute(hash);
      expect(parsed, `${route.key} -> ${hash}`).toBeTruthy();
      // The row that built the URL is the row that reads it back: this is the
      // shadowing guard. /inventory/app/x/logs must not match /inventory/:section.
      expect(parsed.key, `${route.key} -> ${hash}`).toBe(route.key);
      expect(parsed.params).toMatchObject(sample.params);
      expect(parsed.query).toEqual(sample.query);
    });
  });

  it("is idempotent", () => {
    ROUTES.forEach((route) => {
      const once = buildRoute(sampleFor(route));
      const twice = buildRoute(parseRoute(once));
      const thrice = buildRoute(parseRoute(twice));
      expect(twice, route.key).toBe(once);
      expect(thrice, route.key).toBe(once);
    });
  });

  it("omits params that equal their default", () => {
    expect(buildRoute({ key: "resources", params: { tab: "pods" } })).toBe("#/resources");
    expect(buildRoute({ key: "resources", params: { tab: "services" } })).toBe(
      "#/resources/services"
    );
    expect(buildRoute({ key: "alerts", params: { tab: "open" } })).toBe("#/alerts");
    expect(buildRoute({ key: "alerts", params: { tab: "policies" } })).toBe("#/alerts/policies");
  });

  it("fills defaults when the segment is absent", () => {
    expect(parseRoute("#/resources").params.tab).toBe("pods");
    expect(parseRoute("#/alerts").params.tab).toBe("open");
    expect(parseRoute("#/inventory").params.section).toBe("templates");
    expect(parseRoute("#/settings").params.section).toBe("profile");
    expect(parseRoute("#/cluster-builder").params.tab).toBe("floor");
  });
});

describe("param encoding", () => {
  it("survives a slash, percent, space and unicode in an id", () => {
    const nasty = ["a/b", "100%", "with space", "sérvice-π", "a?b#c", "a&b=c"];
    nasty.forEach((value) => {
      const hash = buildRoute({ key: "serviceDetail", params: { serviceId: value } });
      const parsed = parseRoute(hash);
      expect(parsed, value).toBeTruthy();
      expect(parsed.key).toBe("serviceDetail");
      expect(parsed.params.serviceId, `${value} via ${hash}`).toBe(value);
    });
  });

  it("slugifies enumerated values that are not URL-safe", () => {
    const hash = buildRoute({
      key: "applicationIntelligenceDetail",
      params: { appId: "app-1", tab: "Container & build" },
    });
    expect(hash).toBe("#/application-intelligence/app-1/container-build");
    expect(parseRoute(hash).params.tab).toBe("Container & build");
    expect(parseRoute("#/application-intelligence/app-1/apis").params.tab).toBe("APIs");
  });

  it("reads a slug back case-insensitively", () => {
    expect(parseRoute("#/alerts/POLICIES").params.tab).toBe("policies");
    expect(parseRoute("#/resources/configmaps").params.tab).toBe("configMaps");
  });
});

describe("rejecting what is not in the table", () => {
  it("returns null for unknown paths", () => {
    ["", "#", "#/", "#/nope", "#/nope/deeper", "#/dashboard/extra"].forEach((hash) => {
      expect(parseRoute(hash), hash).toBeNull();
    });
  });

  it("returns null for a tab outside its vocabulary", () => {
    expect(parseRoute("#/alerts/bogus")).toBeNull();
    expect(parseRoute("#/resources/bogus")).toBeNull();
    expect(parseRoute("#/settings/bogus")).toBeNull();
    expect(parseRoute("#/cluster-builder/bogus")).toBeNull();
  });

  it("does not let a drill-down be swallowed by its list route", () => {
    expect(parseRoute("#/inventory/app/my-app/logs").key).toBe("applicationDetails");
    expect(parseRoute("#/inventory/helm").key).toBe("inventory");
    expect(parseRoute("#/cluster-builder/builds/42").key).toBe("clusterBuildDetail");
    expect(parseRoute("#/cluster-builder/sources").key).toBe("clusterBuilder");
    expect(parseRoute("#/clusters/prod/overview").key).toBe("clusterOverview");
    expect(parseRoute("#/clusters").key).toBe("clusters");
  });

  it("drops query keys the route does not declare", () => {
    const parsed = parseRoute("#/dashboard?cluster=prod&next=http://evil.example&ns=x");
    expect(parsed.query).toEqual({ cluster: "prod" });
    expect(buildRoute(parsed)).toBe("#/dashboard?cluster=prod");
  });

  it("keeps an injected value as data, never as a path", () => {
    const parsed = parseRoute("#/dashboard?cluster=%3Cscript%3Ealert(1)%3C%2Fscript%3E");
    expect(parsed.query.cluster).toBe("<script>alert(1)</script>");
    expect(buildRoute(parsed)).toBe(
      "#/dashboard?cluster=%3Cscript%3Ealert(1)%3C%2Fscript%3E"
    );
  });

  it("survives a malformed escape sequence", () => {
    expect(() => parseRoute("#/service-catalog/%E0%A4%A")).not.toThrow();
    expect(() => parseRoute("#/dashboard?cluster=%")).not.toThrow();
  });
});

describe("helpers", () => {
  it("hashForPage addresses the list view of a page", () => {
    expect(hashForPage("serviceCatalog")).toBe("#/service-catalog");
    expect(hashForPage("inventory")).toBe("#/inventory");
    expect(hashForPage("mobileApps")).toBe("#/mobile-apps");
    expect(hashForPage("dashboard")).toBe("#/dashboard");
  });

  it("sameRoute compares the address, not the object", () => {
    const a = { key: "resources", params: { tab: "pods" }, query: { cluster: "p" } };
    const b = { key: "resources", params: {}, query: { cluster: "p" } };
    expect(sameRoute(a, b)).toBe(true);
    expect(sameRoute(a, { ...a, query: { cluster: "q" } })).toBe(false);
  });
});
