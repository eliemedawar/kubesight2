import { describe, expect, it } from "vitest";
import { buildAttentionFeed, compareAttention } from "./attentionFeed.js";

const alert = (over = {}) => ({
  id: "a1",
  name: "PodCrashLooping",
  severity: "critical",
  namespace: "payments",
  pod: "api-7c9",
  firedAt: "2026-08-02T11:00:00Z",
  ...over,
});

const integration = (over = {}) => ({
  key: "jira",
  name: "Jira",
  category: "Ticketing",
  status: "degraded",
  message: "Last sync failed",
  lastTestedAt: "2026-08-02T10:00:00Z",
  ...over,
});

const summary = (over = {}) => ({
  clusterId: "prod-eu",
  lastUpdated: "2026-08-02T12:00:00Z",
  nodes: { ready: 3, total: 3 },
  version: { status: "up_to_date" },
  ...over,
});

describe("ranking", () => {
  it("puts critical above warning above info", () => {
    const { items } = buildAttentionFeed({
      alerts: [alert({ id: "w", severity: "warning" }), alert({ id: "c", severity: "critical" })],
      integrations: [integration()],
      approvals: [{ id: "r1", status: "pending", createdAt: "2026-08-02T09:00:00Z" }],
    });
    expect(items.map((i) => i.severity)).toEqual(["critical", "warning", "warning", "info"]);
  });

  it("orders equal severities by recency", () => {
    const { items } = buildAttentionFeed({
      alerts: [
        alert({ id: "old", firedAt: "2026-08-02T09:00:00Z" }),
        alert({ id: "new", firedAt: "2026-08-02T11:00:00Z" }),
      ],
    });
    expect(items.map((i) => i.id)).toEqual(["alert:new", "alert:old"]);
  });

  // Treating a missing date as "now" would float unknowns to the top of every
  // list, which is the opposite of what the evidence supports.
  it("sorts items with no timestamp after ones that have it", () => {
    const withTime = { severity: "critical", detectedAt: "2026-08-02T09:00:00Z", title: "a" };
    const without = { severity: "critical", detectedAt: null, title: "b" };
    expect(compareAttention(withTime, without)).toBeLessThan(0);
    expect(compareAttention(without, withTime)).toBeGreaterThan(0);
  });
});

describe("what each source contributes", () => {
  it("takes only firing critical and warning alerts", () => {
    const { items } = buildAttentionFeed({
      alerts: [alert({ id: "c" }), alert({ id: "i", severity: "info" })],
    });
    expect(items).toHaveLength(1);
    expect(items[0].id).toBe("alert:c");
  });

  it("takes only degraded integrations", () => {
    const { items } = buildAttentionFeed({
      integrations: [
        integration({ key: "jira" }),
        integration({ key: "zoho", status: "connected" }),
        integration({ key: "smtp", status: "not_configured" }),
      ],
    });
    expect(items.map((i) => i.id)).toEqual(["integration:jira"]);
  });

  // A degraded integration does not take the cluster down; ranking it beside a
  // firing critical alert would make the top of the feed less trustworthy.
  it("rates a degraded integration as a warning, not critical", () => {
    const { items } = buildAttentionFeed({ integrations: [integration()] });
    expect(items[0].severity).toBe("warning");
  });

  it("takes only pending approvals", () => {
    const { items } = buildAttentionFeed({
      approvals: [
        { id: "p", status: "pending", createdAt: "2026-08-02T09:00:00Z" },
        { id: "a", status: "approved", createdAt: "2026-08-02T09:00:00Z" },
      ],
    });
    expect(items.map((i) => i.id)).toEqual(["approval:p"]);
  });

  it("says nothing about a healthy cluster", () => {
    const { items } = buildAttentionFeed({ summary: summary() });
    expect(items).toEqual([]);
  });

  it("flags nodes that are not ready", () => {
    const { items } = buildAttentionFeed({ summary: summary({ nodes: { ready: 2, total: 3 } }) });
    expect(items).toHaveLength(1);
    expect(items[0].severity).toBe("warning");
    expect(items[0].title).toBe("1 of 3 nodes not ready");
  });

  // Every node down is an outage, not degraded capacity.
  it("treats every node being down as critical", () => {
    const { items } = buildAttentionFeed({ summary: summary({ nodes: { ready: 0, total: 3 } }) });
    expect(items[0].severity).toBe("critical");
  });

  // Calling every minor version behind a "risk" trains operators to ignore the
  // feed, so only what the backend actually flagged is surfaced.
  it("stays quiet about an up-to-date or unclassified version", () => {
    expect(buildAttentionFeed({ summary: summary() }).items).toEqual([]);
    expect(
      buildAttentionFeed({ summary: summary({ version: { status: "unknown" } }) }).items
    ).toEqual([]);
  });

  it("surfaces an unsupported version", () => {
    const { items } = buildAttentionFeed({
      summary: summary({ version: { status: "unsupported", current: "v1.24.0" } }),
    });
    expect(items).toHaveLength(1);
    expect(items[0].severity).toBe("warning");
  });
});

describe("every item is actionable", () => {
  it("carries severity, scope, time, action and a link", () => {
    const { items } = buildAttentionFeed({
      alerts: [alert()],
      integrations: [integration()],
      approvals: [{ id: "r1", status: "pending", createdAt: "2026-08-02T09:00:00Z" }],
      summary: summary({ nodes: { ready: 1, total: 3 } }),
    });
    expect(items.length).toBeGreaterThan(3);
    items.forEach((entry) => {
      expect(entry.severity, entry.id).toBeTruthy();
      expect(entry.title, entry.id).toBeTruthy();
      expect(entry.action, entry.id).toBeTruthy();
      expect(entry.href, entry.id).toMatch(/^\//);
    });
  });

  it("links an integration straight to its detail screen", () => {
    const { items } = buildAttentionFeed({ integrations: [integration({ key: "zoho" })] });
    expect(items[0].href).toBe("/integrations/zoho");
  });
});

describe("partial data is reported, not hidden", () => {
  // A short feed because a source failed is a different fact from nothing being
  // wrong, and an operator must not read the second when the first is true.
  it("passes through which sources could not be checked", () => {
    const feed = buildAttentionFeed({ alerts: [], unavailable: ["integrations"] });
    expect(feed.items).toEqual([]);
    expect(feed.unavailableSources).toEqual(["integrations"]);
  });

  it("counts by severity", () => {
    const feed = buildAttentionFeed({
      alerts: [alert({ id: "c" }), alert({ id: "w", severity: "warning" })],
    });
    expect(feed.counts).toEqual({ critical: 1, warning: 1 });
  });

  it("reports the true total even when truncated", () => {
    const alerts = Array.from({ length: 10 }, (_, i) => alert({ id: `a${i}` }));
    const feed = buildAttentionFeed({ alerts, limit: 3 });
    expect(feed.items).toHaveLength(3);
    expect(feed.total).toBe(10);
  });

  it("copes with every source missing", () => {
    const feed = buildAttentionFeed();
    expect(feed.items).toEqual([]);
    expect(feed.total).toBe(0);
  });
});
