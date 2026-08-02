import { describe, expect, it } from "vitest";
import {
  CLUSTER_PARAM,
  NAMESPACE_PARAM,
  readScopeFromUrl,
  resolveClusterId,
  resolveNamespace,
  scopeIsPathIdentity,
  scopeSearchParams,
} from "./clusterScope.js";

const CLUSTERS = [{ id: "prod-eu" }, { id: "prod-us" }, { id: "docker-desktop" }];
const NAMESPACES = [{ name: "default" }, { name: "kube-system" }, { name: "payments" }];

describe("where a route carries its scope", () => {
  it("reads cluster and namespace from the path when the route names them", () => {
    expect(
      readScopeFromUrl({
        pageKey: "resources",
        params: { clusterId: "prod-eu", namespace: "payments" },
        searchParams: new URLSearchParams("cluster=ignored&namespace=ignored"),
      })
    ).toEqual({ clusterId: "prod-eu", namespace: "payments" });
  });

  it("reads cluster from the query when the route only filters by it", () => {
    expect(
      readScopeFromUrl({
        pageKey: "alerts",
        params: {},
        searchParams: new URLSearchParams("cluster=prod-us"),
      })
    ).toEqual({ clusterId: "prod-us", namespace: null });
  });

  it("reports null when the URL says nothing, so the caller keeps its selection", () => {
    expect(
      readScopeFromUrl({ pageKey: "alerts", params: {}, searchParams: new URLSearchParams("") })
    ).toEqual({ clusterId: null, namespace: null });
  });

  it("knows which routes treat scope as identity", () => {
    expect(scopeIsPathIdentity("resources")).toBe(true);
    expect(scopeIsPathIdentity("clusterOverview")).toBe(true);
    expect(scopeIsPathIdentity("alerts")).toBe(false);
    expect(scopeIsPathIdentity("settings")).toBe(false);
  });
});

describe("resolveClusterId precedence", () => {
  it("prefers what the URL asks for", () => {
    expect(
      resolveClusterId({
        urlClusterId: "prod-us",
        currentClusterId: "prod-eu",
        defaultClusterId: "docker-desktop",
        clusters: CLUSTERS,
      })
    ).toBe("prod-us");
  });

  // The point of the whole change: a shared link opens on the sender's cluster,
  // not on whatever the reader last had selected.
  it("lets a link override the reader's existing selection", () => {
    expect(
      resolveClusterId({ urlClusterId: "prod-eu", currentClusterId: "prod-us", clusters: CLUSTERS })
    ).toBe("prod-eu");
  });

  it("keeps the current selection when the URL says nothing", () => {
    expect(
      resolveClusterId({ currentClusterId: "prod-us", defaultClusterId: "prod-eu", clusters: CLUSTERS })
    ).toBe("prod-us");
  });

  it("falls back to the workspace default, then the first cluster", () => {
    expect(resolveClusterId({ defaultClusterId: "prod-eu", clusters: CLUSTERS })).toBe("prod-eu");
    expect(resolveClusterId({ clusters: CLUSTERS })).toBe("prod-eu");
  });

  // A bookmark that outlived a permission change is common. Falling through to
  // a cluster the user can see beats pinning an empty selection and rendering a
  // blank page with no explanation.
  it("ignores a cluster the user cannot reach rather than selecting nothing", () => {
    expect(
      resolveClusterId({
        urlClusterId: "decommissioned",
        currentClusterId: "prod-us",
        clusters: CLUSTERS,
      })
    ).toBe("prod-us");
    expect(resolveClusterId({ urlClusterId: "decommissioned", clusters: CLUSTERS })).toBe("prod-eu");
  });

  it("returns empty when there is nothing to select", () => {
    expect(resolveClusterId({ urlClusterId: "prod-eu", clusters: [] })).toBe("");
  });
});

describe("resolveNamespace precedence", () => {
  it("prefers the URL, then the current selection, then the first namespace", () => {
    expect(
      resolveNamespace({ urlNamespace: "payments", currentNamespace: "default", namespaces: NAMESPACES })
    ).toBe("payments");
    expect(resolveNamespace({ currentNamespace: "kube-system", namespaces: NAMESPACES })).toBe(
      "kube-system"
    );
    expect(resolveNamespace({ namespaces: NAMESPACES })).toBe("default");
  });

  // Switching cluster carries the namespace name across; when the new cluster
  // has no such namespace this is what quietly corrects it.
  it("drops a namespace the new cluster does not have", () => {
    expect(resolveNamespace({ urlNamespace: "payments", namespaces: [{ name: "default" }] })).toBe(
      "default"
    );
  });

  it("accepts plain string namespace lists", () => {
    expect(resolveNamespace({ urlNamespace: "b", namespaces: ["a", "b"] })).toBe("b");
  });

  it("returns empty when the cluster has no namespaces", () => {
    expect(resolveNamespace({ urlNamespace: "default", namespaces: [] })).toBe("");
  });
});

describe("what goes into the query string", () => {
  it("writes cluster for a route that filters by it", () => {
    const next = scopeSearchParams({ pageKey: "alerts", clusterId: "prod-eu" });
    expect(next.get(CLUSTER_PARAM)).toBe("prod-eu");
  });

  it("writes namespace only for namespace-scoped routes", () => {
    const logs = scopeSearchParams({ pageKey: "logs", clusterId: "prod-eu", namespace: "payments" });
    expect(logs.get(NAMESPACE_PARAM)).toBe("payments");

    // A namespace on /alerts would be noise in a shared link.
    const alerts = scopeSearchParams({
      pageKey: "alerts",
      clusterId: "prod-eu",
      namespace: "payments",
    });
    expect(alerts.get(NAMESPACE_PARAM)).toBeNull();
  });

  // Implying that /admin/users respects a cluster scope it ignores would be a
  // lie in the address bar.
  it("writes nothing for an unscoped route", () => {
    const next = scopeSearchParams({
      pageKey: "userManagement",
      clusterId: "prod-eu",
      namespace: "payments",
    });
    expect(next.get(CLUSTER_PARAM)).toBeNull();
    expect(next.get(NAMESPACE_PARAM)).toBeNull();
  });

  it("does not duplicate scope a path-identity route already carries", () => {
    const next = scopeSearchParams({
      pageKey: "resources",
      clusterId: "prod-eu",
      namespace: "payments",
    });
    expect(next.get(CLUSTER_PARAM)).toBeNull();
    expect(next.get(NAMESPACE_PARAM)).toBeNull();
  });

  it("preserves unrelated params such as page filters", () => {
    const next = scopeSearchParams({
      pageKey: "alerts",
      searchParams: new URLSearchParams("severity=critical&q=oom"),
      clusterId: "prod-eu",
    });
    expect(next.get("severity")).toBe("critical");
    expect(next.get("q")).toBe("oom");
    expect(next.get(CLUSTER_PARAM)).toBe("prod-eu");
  });

  it("clears a stale scope param when the scope goes away", () => {
    const next = scopeSearchParams({
      pageKey: "alerts",
      searchParams: new URLSearchParams("cluster=prod-eu"),
      clusterId: "",
    });
    expect(next.get(CLUSTER_PARAM)).toBeNull();
  });
});
