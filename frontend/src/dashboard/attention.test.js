import { describe, expect, it } from "vitest";
import { buildAttentionItems, formatBytes, usageTone } from "./attention.js";

const GIB = 1024 ** 3;

describe("buildAttentionItems", () => {
  it("is empty for a healthy cluster", () => {
    const items = buildAttentionItems({
      health: { status: "healthy" },
      nodeHealth: [{ name: "n1", status: "healthy", issues: [] }],
      problemPods: [],
      pods: { failed: 0 },
      alerts: { warning: 0 },
      topAlerts: [],
      version: { status: "up_to_date" },
    });
    expect(items).toEqual([]);
  });

  it("only reports an unreachable cluster, nothing stale under it", () => {
    const items = buildAttentionItems({
      health: { status: "unreachable", reasons: ["Cluster is offline or unreachable"] },
      nodeHealth: [{ name: "n1", status: "critical", issues: ["Not ready"] }],
    });
    expect(items.map((i) => i.id)).toEqual(["cluster:unreachable"]);
  });

  it("ranks danger before warn before info and keeps category order inside a tone", () => {
    const items = buildAttentionItems({
      nodeHealth: [
        { name: "warm", status: "warning", issues: ["Disk 87%"] },
        { name: "down", status: "critical", issues: ["Not ready"], cordoned: true },
        { name: "maint", status: "healthy", issues: [], cordoned: true },
      ],
      problemPods: [{ name: "api-1", namespace: "payments", status: "CrashLoopBackOff" }],
      problemPodsTotal: 1,
      alerts: { warning: 2 },
      topAlerts: [
        { id: "a1", severity: "critical", title: "High CPU", namespace: "payments" },
        { id: "a2", severity: "warning", title: "Memory creeping" },
      ],
      version: { status: "two_minor_versions_behind", current: "v1.29.4", latest: "v1.31.2" },
    });
    expect(items.map((i) => i.id)).toEqual([
      "node:down",
      "pod:payments/api-1",
      "alert:a1",
      "node:warm",
      "alerts:warning",
      "version",
      "node:maint",
    ]);
    expect(items[0].detail).toBe("Not ready · cordoned");
    expect(items[1].target.options.query).toEqual({ ns: "payments" });
  });

  it("folds failing pods past three into one row with the right count", () => {
    const pods = Array.from({ length: 5 }, (_, i) => ({ name: `p${i}`, namespace: "ns", status: "Error" }));
    const items = buildAttentionItems({ problemPods: pods, problemPodsTotal: 9 });
    expect(items.map((i) => i.id)).toEqual(["pod:ns/p0", "pod:ns/p1", "pods:more"]);
    expect(items[2].title).toBe("7 more pods failing");
  });

  it("falls back to the failed count when pod names are not known", () => {
    const items = buildAttentionItems({ problemPods: [], pods: { failed: 3 } });
    expect(items).toHaveLength(1);
    expect(items[0].title).toBe("3 pods failing");
  });

  it("ignores a single minor version behind", () => {
    expect(buildAttentionItems({ version: { status: "one_minor_version_behind" } })).toEqual([]);
  });
});

describe("formatBytes / usageTone", () => {
  it("formats GiB and TiB and never invents a number", () => {
    expect(formatBytes(48.2 * GIB)).toBe("48.2 GiB");
    expect(formatBytes(181.5 * GIB)).toBe("182 GiB");
    expect(formatBytes(2048 * GIB)).toBe("2.0 TiB");
    expect(formatBytes(null)).toBe("—");
  });

  it("colors usage at 85 and 90 percent", () => {
    expect(usageTone(50)).toBe("ok");
    expect(usageTone(85)).toBe("warn");
    expect(usageTone(90)).toBe("danger");
    expect(usageTone(null)).toBe("muted");
  });
});
