import { describe, expect, it } from "vitest";
import { getVisiblePages } from "../../utils/authz.js";
import { RESULT_KIND, score, searchCommands } from "./commandSearch.js";

const ADMIN = { isAdmin: true, hasFullAccess: true, role: "admin", permissions: [], accessRules: [] };
const AUDIT_ONLY = {
  isAdmin: false,
  role: "user",
  permissions: ["audit:view"],
  accessRules: [],
  clusterAccess: [],
};

const CLUSTERS = [{ id: "prod-eu", name: "Production EU" }, { id: "docker-desktop" }];
const NAMESPACES = [{ name: "payments" }, { name: "kube-system" }];

describe("scoring", () => {
  it("ranks exact above prefix above word-start above substring", () => {
    expect(score("alerts", "alerts")).toBeGreaterThan(score("alerts feed", "alerts"));
    expect(score("alerts feed", "alerts")).toBeGreaterThan(score("open alerts", "alerts"));
    expect(score("open alerts", "alerts")).toBeGreaterThan(score("thealertsx", "alerts"));
  });

  it("reports no match rather than a weak one", () => {
    expect(score("clusters", "zzz")).toBe(-1);
  });

  it("is case-insensitive", () => {
    expect(score("Audit Logs", "audit")).toBeGreaterThan(0);
  });
});

describe("permission awareness", () => {
  // A search box that reveals a page exists by name is an information leak even
  // when the link 403s, so results are built from the permitted set rather than
  // filtered afterwards.
  it("never surfaces a page the user cannot reach", () => {
    const results = searchCommands("users", { visiblePages: getVisiblePages(AUDIT_ONLY) });
    expect(results).toEqual([]);
  });

  it("surfaces a page the user can reach", () => {
    const results = searchCommands("audit", { visiblePages: getVisiblePages(AUDIT_ONLY) });
    expect(results[0]).toMatchObject({ kind: RESULT_KIND.PAGE, href: "/admin/audit" });
  });

  it("searches an admin's full set", () => {
    const results = searchCommands("integrations", { visiblePages: getVisiblePages(ADMIN) });
    expect(results[0].href).toBe("/integrations");
  });
});

describe("what is searchable", () => {
  it("finds clusters by name and by id", () => {
    const byName = searchCommands("production", {
      visiblePages: getVisiblePages(ADMIN),
      clusters: CLUSTERS,
    });
    expect(byName[0]).toMatchObject({ kind: RESULT_KIND.CLUSTER, href: "/fleet/clusters/prod-eu" });

    const byId = searchCommands("docker", {
      visiblePages: getVisiblePages(ADMIN),
      clusters: CLUSTERS,
    });
    expect(byId[0].href).toBe("/fleet/clusters/docker-desktop");
  });

  it("finds namespaces within the selected cluster", () => {
    const results = searchCommands("payments", {
      visiblePages: getVisiblePages(ADMIN),
      namespaces: NAMESPACES,
      clusterId: "prod-eu",
    });
    expect(results[0]).toMatchObject({
      kind: RESULT_KIND.NAMESPACE,
      href: "/workloads/prod-eu/payments",
    });
  });

  // Without a cluster there is no valid namespace URL to build, and offering a
  // result that cannot navigate is worse than offering none.
  it("offers no namespaces when no cluster is selected", () => {
    const results = searchCommands("payments", {
      visiblePages: getVisiblePages(ADMIN),
      namespaces: NAMESPACES,
      clusterId: "",
    });
    expect(results.every((r) => r.kind !== RESULT_KIND.NAMESPACE)).toBe(true);
  });

  it("finds an area by its group name", () => {
    const results = searchCommands("operate", { visiblePages: getVisiblePages(ADMIN) });
    expect(results.length).toBeGreaterThan(0);
    expect(results.every((r) => r.kind === RESULT_KIND.PAGE)).toBe(true);
  });
});

describe("empty query", () => {
  // Offering every namespace in a large cluster before anything is typed is
  // noise, so the resting state is a jump list of pages.
  it("returns pages only, as a jump list", () => {
    const results = searchCommands("", {
      visiblePages: getVisiblePages(ADMIN),
      clusters: CLUSTERS,
      namespaces: NAMESPACES,
      clusterId: "prod-eu",
    });
    expect(results.length).toBeGreaterThan(0);
    expect(results.every((r) => r.kind === RESULT_KIND.PAGE)).toBe(true);
  });

  it("returns nothing at all for a user with no pages", () => {
    expect(searchCommands("", { visiblePages: [] })).toEqual([]);
  });
});

describe("results are navigable", () => {
  it("gives every result an href", () => {
    const results = searchCommands("a", {
      visiblePages: getVisiblePages(ADMIN),
      clusters: CLUSTERS,
      namespaces: NAMESPACES,
      clusterId: "prod-eu",
    });
    expect(results.length).toBeGreaterThan(0);
    results.forEach((result) => expect(result.href).toMatch(/^\//));
  });

  it("respects the limit", () => {
    const results = searchCommands("a", { visiblePages: getVisiblePages(ADMIN), limit: 3 });
    expect(results.length).toBeLessThanOrEqual(3);
  });
});
