import { describe, expect, it } from "vitest";
import { computeLayout, NODE_W } from "./TopologyViewer.jsx";


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
});
