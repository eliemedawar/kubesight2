import { describe, expect, it } from "vitest";
import {
  appItemsFromBuild,
  appsError,
  appsPayload,
  moveItem,
  newAppItem,
  releaseNameFor,
  unfilledRequired,
} from "./clusterApps.js";

describe("releaseNameFor", () => {
  it("makes a DNS-safe name from a chart id", () => {
    expect(releaseNameFor("Areeba_TXM chart")).toBe("areeba-txm-chart");
    expect(releaseNameFor("x".repeat(80))).toHaveLength(53);
  });
});

describe("appsError", () => {
  const item = { ...newAppItem({ id: "web", version: "1.0.0" }), namespace: "shop" };

  it("accepts a complete item", () => {
    expect(appsError([item])).toBe("");
  });

  it("asks for a chart, a namespace and a release", () => {
    expect(appsError([{ ...item, chartSlug: "" }])).toMatch(/pick a Helm chart/);
    expect(appsError([{ ...item, namespace: "" }])).toMatch(/namespace/);
    expect(appsError([{ ...item, releaseName: "" }])).toMatch(/release name/);
  });

  it("refuses names Kubernetes and Helm would refuse", () => {
    expect(appsError([{ ...item, namespace: "Shop_1" }])).toMatch(/not a valid namespace/);
    expect(appsError([{ ...item, releaseName: "-web" }])).toMatch(/not a valid release/);
  });

  it("refuses one release twice in a namespace, including installed ones", () => {
    expect(appsError([item, { ...item, key: "b" }])).toMatch(/already in namespace shop/);
    expect(appsError([item], [{ namespace: "shop", releaseName: "web" }])).toMatch(/already/);
    expect(appsError([item, { ...item, namespace: "other" }])).toBe("");
  });
});

describe("appsPayload / appItemsFromBuild", () => {
  it("round-trips a saved chart, keeping its id and masked values", () => {
    const build = {
      apps: [
        { id: 1, status: "installed", chartSlug: "a", namespace: "n", releaseName: "a" },
        {
          id: 2, status: "failed", chartSlug: "web", chartVersion: "1.2.0",
          valuesFile: "values-uat.yaml", namespace: "shop", releaseName: "web",
          valuesYaml: "database:\n  password: <hidden>\n",
        },
      ],
    };
    const items = appItemsFromBuild(build);
    expect(items).toHaveLength(1);
    expect(items[0].valuesEdited).toBe(true);
    const [payload] = appsPayload(items);
    expect(payload).toEqual({
      id: 2, chartSlug: "web", chartVersion: "1.2.0", valuesFile: "values-uat.yaml",
      namespace: "shop", releaseName: "web",
      valuesYaml: "database:\n  password: <hidden>\n",
    });
  });
});

describe("moveItem", () => {
  it("moves within bounds only", () => {
    expect(moveItem([1, 2, 3], 0, 1)).toEqual([2, 1, 3]);
    expect(moveItem([1, 2, 3], 0, -1)).toEqual([1, 2, 3]);
    expect(moveItem([1, 2, 3], 2, 1)).toEqual([1, 2, 3]);
  });
});

describe("unfilledRequired", () => {
  const required = [{ path: "database.password" }, { path: "api.token" }];

  it("lists required keys that are absent or blank", () => {
    const text = 'database:\n  password: ""\napi:\n  token: abc\n';
    expect(unfilledRequired(text, required).map((r) => r.path)).toEqual(["database.password"]);
  });

  it("counts a masked stored value as filled", () => {
    expect(unfilledRequired("database:\n  password: <hidden>\napi:\n  token: x\n", required)).toEqual([]);
  });
});
