import { describe, expect, it } from "vitest";
import {
  NAMESPACE_RESOURCE_LIST_KEYS,
  RESOURCE_GROUPS,
  RESOURCE_TAB_DEFINITIONS,
  emptyNamespaceResources,
  listKeyHasTone,
  rowTone,
  statusTone,
  tabKeyForApiKind,
  tabKeyForListKey,
} from "./resourceTypes.js";
import { getAllowedResources } from "../utils/authz.js";

describe("resource kinds", () => {
  it("puts every tab in exactly one rail group", () => {
    const grouped = RESOURCE_GROUPS.flatMap((group) => group.tabs);
    expect(new Set(grouped).size).toBe(grouped.length);
    expect([...grouped].sort()).toEqual(RESOURCE_TAB_DEFINITIONS.map((def) => def.tabKey).sort());
  });

  it("gives every tab a list key the page can load", () => {
    for (const def of RESOURCE_TAB_DEFINITIONS) {
      expect(NAMESPACE_RESOURCE_LIST_KEYS).toContain(def.listKey);
      expect(typeof def.describe("payments")).toBe("string");
    }
    expect(Object.keys(emptyNamespaceResources())).toEqual(NAMESPACE_RESOURCE_LIST_KEYS);
  });

  it("keeps Pods as the first tab (the fallback for a tab the user cannot see)", () => {
    expect(RESOURCE_TAB_DEFINITIONS[0].tabKey).toBe("pods");
  });

  it("maps Kubernetes kinds and list keys back to tabs", () => {
    expect(tabKeyForApiKind("PersistentVolumeClaim")).toBe("pvcs");
    expect(tabKeyForApiKind("HorizontalPodAutoscaler")).toBe("hpas");
    expect(tabKeyForApiKind("Lease")).toBe("");
    expect(tabKeyForListKey("networkpolicies")).toBe("networkPolicies");
    expect(tabKeyForListKey("ingress")).toBe("ingresses");
  });
});

// Mirrors backend/tests/test_resource_kinds_extra.py: the server counts the
// rail badges with the same rules this filter applies.
describe("what needs attention", () => {
  it("reads kubectl statuses", () => {
    expect(statusTone("Running")).toBe("");
    expect(statusTone("Complete")).toBe("");
    expect(statusTone("CrashLoopBackOff")).toBe("crit");
    expect(statusTone("Failed")).toBe("crit");
    expect(statusTone("Pending")).toBe("warn");
    expect(statusTone("warning")).toBe("warn");
    expect(statusTone("-")).toBe("");
  });

  it("uses the status field each kind carries", () => {
    expect(rowTone("pods", { status: "ImagePullBackOff" }, "ns")).toBe("crit");
    expect(rowTone("deployments", { updateStatus: "warning", status: "healthy" }, "ns")).toBe("warn");
    expect(rowTone("jobs", { status: "Complete" }, "ns")).toBe("");
    expect(rowTone("configmaps", { status: "Pending" }, "ns")).toBe("");
  });

  it("uses the computed health of claims, volumes and autoscalers", () => {
    expect(rowTone("pvcs", { health: "warn" }, "ns")).toBe("warn");
    expect(rowTone("hpas", { health: "ok" }, "ns")).toBe("");
    expect(rowTone("pvs", { health: "warn", claimNamespace: "ns" }, "ns")).toBe("warn");
    // Another namespace's released volume is not this namespace's problem.
    expect(rowTone("pvs", { health: "warn", claimNamespace: "other" }, "ns")).toBe("");
  });

  it("counts warning events", () => {
    expect(rowTone("events", { type: "Warning" }, "ns")).toBe("warn");
    expect(rowTone("events", { type: "Normal" }, "ns")).toBe("");
  });

  it("offers the attention filter only where rows can need attention", () => {
    expect(listKeyHasTone("pods")).toBe(true);
    expect(listKeyHasTone("pvcs")).toBe(true);
    expect(listKeyHasTone("configmaps")).toBe(false);
    expect(listKeyHasTone("storageclasses")).toBe(false);
  });
});

describe("getAllowedResources for the newer kinds", () => {
  const viewer = {
    permissions: ["clusters:view", "namespaces:view", "resources:view", "deployments:view"],
    clusterAccess: ["c1"],
    namespaceAccess: [{ clusterId: "c1", namespace: "payments" }],
  };

  it("lets namespace kinds through for a reachable namespace", () => {
    const allowed = getAllowedResources(viewer, "c1", "payments", {
      configmaps: [{ name: "cfg" }],
      secrets: [{ name: "s" }],
      pvcs: [{ name: "data" }],
      networkpolicies: [{ name: "deny" }],
      storageclasses: [{ name: "gp3" }],
      events: [{ type: "Warning" }],
    });
    expect(allowed.configmaps).toHaveLength(1);
    expect(allowed.secrets).toHaveLength(1);
    expect(allowed.pvcs).toHaveLength(1);
    expect(allowed.networkpolicies).toHaveLength(1);
    expect(allowed.storageclasses).toHaveLength(1);
    expect(allowed.events).toHaveLength(1);
  });

  it("hides them for a namespace the user cannot reach", () => {
    const allowed = getAllowedResources(viewer, "c1", "kube-system", {
      pvcs: [{ name: "data" }],
      configmaps: [{ name: "cfg" }],
    });
    expect(allowed.pvcs).toEqual([]);
    expect(allowed.configmaps).toEqual([]);
  });

  it("shows only volumes claimed from reachable namespaces", () => {
    const allowed = getAllowedResources(viewer, "c1", "payments", {
      pvs: [
        { name: "mine", claimNamespace: "payments" },
        { name: "theirs", claimNamespace: "kube-system" },
        { name: "free", claimNamespace: "" },
      ],
    });
    expect(allowed.pvs.map((pv) => pv.name)).toEqual(["mine"]);
  });

  it("gives admins everything", () => {
    const allowed = getAllowedResources({ isAdmin: true }, "c1", "x", {
      pvs: [{ name: "free", claimNamespace: "" }],
    });
    expect(allowed.pvs).toHaveLength(1);
  });
});
