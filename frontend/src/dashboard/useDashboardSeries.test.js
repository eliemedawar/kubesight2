import { describe, expect, it } from "vitest";
import { nextSeriesBuffer } from "./useDashboardSeries.js";

describe("nextSeriesBuffer", () => {
  it("starts empty and only holds real readings", () => {
    const first = nextSeriesBuffer(null, { signature: "c1:6h", cpuVal: 42, memVal: 60, points: 3 });
    expect(first.cpu).toEqual([42]);
    expect(first.mem).toEqual([60]);
  });

  it("skips unavailable readings instead of inventing values", () => {
    const b = nextSeriesBuffer(null, { signature: "c1:6h", cpuVal: null, memVal: Number.NaN, points: 3 });
    expect(b.cpu).toEqual([]);
    expect(b.mem).toEqual([]);
  });

  it("caps at the range capacity and resets on cluster/range change", () => {
    let b = null;
    for (const v of [1, 2, 3, 4]) {
      b = nextSeriesBuffer(b, { signature: "c1:1h", cpuVal: v, memVal: v, points: 3 });
    }
    expect(b.cpu).toEqual([2, 3, 4]);
    b = nextSeriesBuffer(b, { signature: "c2:1h", cpuVal: 9, memVal: null, points: 3 });
    expect(b.cpu).toEqual([9]);
    expect(b.mem).toEqual([]);
  });
});
