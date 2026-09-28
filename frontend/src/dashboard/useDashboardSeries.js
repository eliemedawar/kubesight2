import { useEffect, useMemo, useRef, useState } from "react";

// Number of points held per series for each time range (1h → 60, 6h → 72,
// 24h → 96). This is the buffer's capacity, not a promise of history: the
// buffer starts empty and fills one real sample per dashboard poll.
const RANGE_POINTS = { "1h": 60, "6h": 72, "24h": 96 };
export const TIME_RANGES = ["1h", "6h", "24h"];

// Distribute a single cluster CPU% value into up to three stacked bands using
// each namespace's pod share, so the stacked chart reflects the real total while
// approximating per-namespace contribution. Falls back to a single band.
function cpuBands(namespaces) {
  const ranked = [...(namespaces || [])]
    .filter((ns) => (ns.pods ?? 0) > 0)
    .sort((a, b) => (b.pods ?? 0) - (a.pods ?? 0))
    .slice(0, 3);
  if (!ranked.length) {
    return [{ label: "cluster", weight: 1 }];
  }
  const total = ranked.reduce((sum, ns) => sum + (ns.pods ?? 0), 0) || 1;
  return ranked.map((ns) => ({ label: ns.name, weight: (ns.pods ?? 0) / total }));
}

// Pure buffer step, exported for tests. Returns the next buffer given the
// previous one and a new summary reading. Only real readings are appended —
// nothing is ever generated or seeded — and the buffer resets (to empty) when
// the cluster or range changes.
export function nextSeriesBuffer(buffer, { signature, cpuVal, memVal, points }) {
  const prev = buffer && buffer.signature === signature ? buffer : { signature, cpu: [], mem: [] };
  const push = (arr, val) => {
    if (val == null || !Number.isFinite(val)) return arr;
    const next = [...arr, val];
    while (next.length > points) next.shift();
    return next;
  };
  return {
    signature,
    cpu: push(prev.cpu, cpuVal),
    mem: push(prev.mem, memVal),
  };
}

// useDashboardSeries turns the point-in-time dashboard summary into rolling
// time-series suitable for the canvas charts.
//
// The summary API returns instantaneous values (cpuUsage.percent, etc.), not
// history, so we keep a short client-side buffer: on each new summary we append
// the latest real CPU/Memory reading and shift the oldest out. History starts
// empty and only ever holds real polls. There is no network metrics source, so
// the network series are always empty (netAvailable: false) and the panel says so.
export function useDashboardSeries(summary, range = "6h") {
  const points = RANGE_POINTS[range] || RANGE_POINTS["6h"];
  const clusterId = summary?.clusterId;
  const cpu = summary?.cpuUsage;
  const mem = summary?.memoryUsage;
  const sampledAt = summary?.lastUpdated;

  const bufferRef = useRef({ signature: "", cpu: [], mem: [] });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!summary) return;
    const cpuVal = cpu?.available ? Number(cpu.percent) : null;
    const memVal = mem?.available ? Number(mem.percent) : null;
    bufferRef.current = nextSeriesBuffer(bufferRef.current, {
      signature: `${clusterId || ""}:${range}`,
      cpuVal,
      memVal,
      points,
    });
    setTick((t) => t + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId, range, sampledAt, points]);

  const bands = useMemo(() => cpuBands(summary?.namespaces), [summary?.namespaces]);

  return useMemo(() => {
    const buffer = bufferRef.current;
    const cpuHistory = buffer.cpu;
    return {
      // CPU split into stacked namespace bands (real total, split estimated
      // from each namespace's pod share).
      cpuBands: bands.map((band) => ({
        label: band.label,
        data: cpuHistory.map((v) => v * band.weight),
      })),
      cpu: cpuHistory,
      mem: buffer.mem,
      netIn: [],
      netOut: [],
      memLimit: 85,
      cpuReal: Boolean(cpu?.available),
      memReal: Boolean(mem?.available),
      // No network metrics source exists yet; panels must render an explicit
      // empty state instead of numbers.
      netAvailable: false,
    };
    // Recompute the returned snapshot whenever a new sample lands (tick).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sampledAt, clusterId, range, points, bands, cpu?.available, mem?.available, tick]);
}
