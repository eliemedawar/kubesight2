import { useEffect, useMemo, useRef, useState } from "react";

// Number of points held per series for each time range. Mirrors the reference
// mock (1h → 60, 6h → 72, 24h → 96) so the chart density matches the design.
const RANGE_POINTS = { "1h": 60, "6h": 72, "24h": 96 };
export const TIME_RANGES = ["1h", "6h", "24h"];

// Deterministic seeded RNG (same Lehmer generator as the reference) so the
// seeded/fallback history is stable across renders instead of jittering.
function rng(seed) {
  let s = seed || 1;
  return () => {
    s = (s * 16807) % 2147483647;
    return s / 2147483647;
  };
}

// Random-walk series generator, used to seed history before enough real samples
// have accumulated and to drive the network panel (for which the summary API
// exposes no real metric yet).
function walk(n, base, vol, min, max, r) {
  let v = base;
  const out = [];
  for (let i = 0; i < n; i += 1) {
    v += (r() - 0.5) * vol;
    v = Math.max(min, Math.min(max, v));
    out.push(v);
  }
  return out;
}

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

// useDashboardSeries turns the point-in-time dashboard summary into rolling
// time-series suitable for the canvas charts.
//
// The summary API returns instantaneous values (cpuUsage.percent, etc.), not
// history, so we keep a short client-side buffer: on each new summary we append
// the latest real CPU/Memory reading and shift the oldest out. Until enough real
// readings exist the buffer is seeded with a random walk around the current
// value, and the buffer resets when the cluster or range changes.
export function useDashboardSeries(summary, range = "6h") {
  const points = RANGE_POINTS[range] || RANGE_POINTS["6h"];
  const clusterId = summary?.clusterId;
  const cpu = summary?.cpuUsage;
  const mem = summary?.memoryUsage;
  const sampledAt = summary?.lastUpdated;

  const bufferRef = useRef({ signature: "", cpu: [], mem: [], netIn: [], netOut: [] });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!summary) return;
    const buffer = bufferRef.current;
    const signature = `${clusterId || ""}:${range}`;
    const cpuVal = cpu?.available ? Number(cpu.percent) || 0 : null;
    const memVal = mem?.available ? Number(mem.percent) || 0 : null;

    // Reset + seed when switching cluster or range, or on first run.
    if (buffer.signature !== signature) {
      const r = rng((clusterId || "seed").length * 7 + points);
      buffer.signature = signature;
      buffer.cpu = walk(points, cpuVal ?? 24, 5, 4, 96, r);
      buffer.mem = walk(points, memVal ?? 42, 4, 6, 96, r);
      buffer.netIn = walk(points, 720, 120, 80, 1500, r);
      buffer.netOut = walk(points, 540, 110, 60, 1400, r);
      // Pin the most recent seeded point to the real reading when available.
      if (cpuVal != null) buffer.cpu[points - 1] = cpuVal;
      if (memVal != null) buffer.mem[points - 1] = memVal;
    } else {
      // Append the newest real reading and keep the network sample advancing.
      const r = rng(Math.floor(Date.now() / 1000) % 2147483647 || 1);
      const push = (arr, val) => {
        arr.push(val);
        while (arr.length > points) arr.shift();
      };
      if (cpuVal != null) push(buffer.cpu, cpuVal);
      if (memVal != null) push(buffer.mem, memVal);
      const lastIn = buffer.netIn[buffer.netIn.length - 1] ?? 720;
      const lastOut = buffer.netOut[buffer.netOut.length - 1] ?? 540;
      push(buffer.netIn, Math.max(80, Math.min(1500, lastIn + (r() - 0.5) * 240)));
      push(buffer.netOut, Math.max(60, Math.min(1400, lastOut + (r() - 0.5) * 220)));
    }
    setTick((t) => t + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId, range, sampledAt, points]);

  const bands = useMemo(() => cpuBands(summary?.namespaces), [summary?.namespaces]);

  return useMemo(() => {
    const buffer = bufferRef.current;
    const cpuHistory = buffer.cpu.length ? buffer.cpu : new Array(points).fill(0);
    return {
      // CPU split into stacked namespace bands (real total, approximated split).
      cpuBands: bands.map((band) => ({
        label: band.label,
        data: cpuHistory.map((v) => v * band.weight),
      })),
      cpu: cpuHistory,
      mem: buffer.mem.length ? buffer.mem : new Array(points).fill(0),
      netIn: buffer.netIn,
      netOut: buffer.netOut,
      memLimit: 85,
      // Flags so panels can label which series is real vs. sample data.
      cpuReal: Boolean(cpu?.available),
      memReal: Boolean(mem?.available),
      netReal: false,
    };
    // Recompute the returned snapshot whenever a new sample lands AND right
    // after the seeding effect fills the buffer (tick), so the line shows up on
    // first paint instead of waiting for the next poll to change sampledAt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sampledAt, clusterId, range, points, bands, cpu?.available, mem?.available, tick]);
}
