import { describe, expect, it } from "vitest";
import { computeLayout, MAX_ZOOM, NODE_H, NODE_W } from "./TopologyViewer.jsx";


describe("computeLayout", () => {
  it("wraps broad graph layers so large clusters remain readable", () => {
    const nodes = [
      { id: "root" },
      ...Array.from({ length: 12 }, (_, index) => ({ id: `leaf-${index}` })),
    ];
    const edges = nodes.slice(1).map((node) => ({
      sourceNodeId: "root",
      targetNodeId: node.id,
    }));

    const layout = computeLayout(nodes, edges);
    const leafPositions = nodes.slice(1).map((node) => layout.positions[node.id]);
    const distinctRows = new Set(leafPositions.map((position) => position.y));

    expect(distinctRows.size).toBe(3);
    expect(layout.svgW).toBeLessThan(6 * (NODE_W + 70));
    expect(Math.max(...leafPositions.map((position) => position.y))).toBeGreaterThan(
      Math.min(...leafPositions.map((position) => position.y))
    );
  });

  it("packs many independent traffic paths into a widescreen overview", () => {
    const nodes = [];
    const edges = [];
    for (let index = 0; index < 40; index += 1) {
      nodes.push(
        { id: `service-${index}`, name: `service-${index}`, kind: "service" },
        { id: `pod-${index}`, name: `pod-${index}`, kind: "pod" }
      );
      edges.push({
        sourceNodeId: `service-${index}`,
        targetNodeId: `pod-${index}`,
        kind: "routes",
      });
    }

    const layout = computeLayout(nodes, edges, "packed");
    const fitScale = Math.min(1920 / layout.svgW, 900 / layout.svgH);

    expect(NODE_H * fitScale).toBeGreaterThanOrEqual(24);
    edges.forEach((edge) => {
      const source = layout.positions[edge.sourceNodeId];
      const target = layout.positions[edge.targetNodeId];
      expect(target.x - source.x).toBeGreaterThanOrEqual(NODE_W);
      expect(Math.abs(target.y - source.y)).toBeLessThan(NODE_H);
    });
  });

  it("allows zooming well beyond the old five-times cap", () => {
    expect(MAX_ZOOM).toBeGreaterThanOrEqual(20);
  });
});
