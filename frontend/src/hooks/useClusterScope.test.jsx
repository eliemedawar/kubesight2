// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { matchPath } from "../routes/paths.js";
import { useClusterScope } from "./useClusterScope.js";

/**
 * The pure precedence rules are covered in routes/clusterScope.test.js. What
 * needs a real router is the binding itself: that arriving at a URL selects the
 * scope it names, and that changing the selector puts it back in the address.
 */

afterEach(cleanup);

const CLUSTERS = [{ id: "prod-eu" }, { id: "prod-us" }];
const NAMESPACES = [{ name: "default" }, { name: "payments" }];

let scopeApi = null;
let goBack = null;

function Harness() {
  const location = useLocation();
  const navigate = useNavigate();
  goBack = () => navigate(-1);
  const match = matchPath(location.pathname);
  const [selectedClusterId, setSelectedClusterId] = useState("");
  const [selectedNamespace, setSelectedNamespace] = useState("");

  scopeApi = useClusterScope({
    pageKey: match?.pageKey || "",
    routeParams: match?.params,
    clusters: CLUSTERS,
    namespaces: NAMESPACES,
    defaultClusterId: "",
    selectedClusterId,
    selectedNamespace,
    onClusterChange: setSelectedClusterId,
    onNamespaceChange: setSelectedNamespace,
  });

  return (
    <div>
      <span data-testid="cluster">{selectedClusterId}</span>
      <span data-testid="namespace">{selectedNamespace}</span>
      <span data-testid="url">{`${location.pathname}${location.search}`}</span>
    </div>
  );
}

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<Harness />} />
      </Routes>
    </MemoryRouter>
  );
}

const cluster = () => screen.getByTestId("cluster").textContent;
const namespace = () => screen.getByTestId("namespace").textContent;
const url = () => screen.getByTestId("url").textContent;

describe("arriving at a URL", () => {
  it("selects the cluster and namespace named in the path", () => {
    renderAt("/workloads/prod-us/payments");
    expect(cluster()).toBe("prod-us");
    expect(namespace()).toBe("payments");
  });

  it("selects the cluster named in the query string", () => {
    renderAt("/alerts?cluster=prod-us");
    expect(cluster()).toBe("prod-us");
  });

  it("falls back to a reachable cluster when the URL names an unknown one", () => {
    renderAt("/alerts?cluster=decommissioned");
    expect(cluster()).toBe("prod-eu");
  });

  it("picks a default when the URL names no scope", () => {
    renderAt("/alerts");
    expect(cluster()).toBe("prod-eu");
  });

  it("selects nothing on a route that takes no cluster", () => {
    renderAt("/admin/users");
    expect(cluster()).toBe("");
  });
});

describe("changing the selector writes the URL", () => {
  it("puts the cluster in the query string on a filtering route", () => {
    renderAt("/alerts");
    act(() => scopeApi.setCluster("prod-us"));
    expect(cluster()).toBe("prod-us");
    expect(url()).toContain("cluster=prod-us");
    // A filter change should not restructure the path.
    expect(url()).toContain("/alerts");
  });

  it("navigates on a route whose path names the cluster", () => {
    renderAt("/workloads/prod-eu/payments");
    act(() => scopeApi.setCluster("prod-us"));
    expect(url()).toBe("/workloads/prod-us/payments");
  });

  it("navigates when the namespace changes on a path-identity route", () => {
    renderAt("/workloads/prod-eu/payments");
    act(() => scopeApi.setNamespace("default"));
    expect(url()).toBe("/workloads/prod-eu/default");
  });

  it("keeps unrelated filters in the query string", () => {
    renderAt("/alerts?severity=critical");
    act(() => scopeApi.setCluster("prod-us"));
    expect(url()).toContain("severity=critical");
    expect(url()).toContain("cluster=prod-us");
  });

  it("ignores a no-op change", () => {
    renderAt("/workloads/prod-eu/payments");
    const before = url();
    act(() => scopeApi.setCluster("prod-eu"));
    expect(url()).toBe(before);
  });
});

describe("scope survives navigation", () => {
  // The reason cluster went into the URL at all: back should restore what you
  // were looking at, not just which page you were on.
  // MemoryRouter keeps its own history, so this goes through the router rather
  // than window.history — which is a no-op inside it.
  it("restores the previous cluster when going back", () => {
    renderAt("/workloads/prod-eu/payments");
    act(() => scopeApi.setCluster("prod-us"));
    expect(url()).toBe("/workloads/prod-us/payments");

    act(() => goBack());
    expect(url()).toBe("/workloads/prod-eu/payments");
    expect(cluster()).toBe("prod-eu");
  });

  it("restores a query-carried cluster when going back", () => {
    renderAt("/alerts");
    act(() => scopeApi.setCluster("prod-us"));
    expect(cluster()).toBe("prod-us");

    act(() => goBack());
    expect(cluster()).toBe("prod-eu");
  });
});
