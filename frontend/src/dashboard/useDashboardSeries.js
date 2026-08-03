import { useEffect, useMemo, useRef, useState } from "react";

export const TIME_RANGES = ["1h", "6h", "24h"];

// How many observations to keep per range. The summary API returns
// instantaneous values, not history, so this is a buffer of what this browser
// has actually watched happen — it is not a time window, and the labels say so.
const RANGE_POINTS = { "1h": 60, "6h": 72, "24h": 96 };

/**
 * Rolling history of *observed* readings.
 *
 * This replaces a random-walk generator. That generator seeded the CPU and
 * memory charts with a fabricated history whenever there were not yet enough
 * real samples, invented the entire network series (there is no API for it),
 * split a single cluster CPU total into per-namespace bands by pod count, and
 * drew a "limit" line at a hardcoded 85%. All of it rendered identically to
 * real data, at operator-facing resolution, on a page whose job is to answer
 * "is this cluster healthy right now".
 *
 * A fabricated number an operator trusts is worse than a blank that tells the
 * truth, and the failure mode here is specific: a seeded walk around the
 * current value always looks like a stable cluster, so the one thing a chart is
 * for — noticing that something changed — is exactly what it could not show.
 * Phase 2 replaces this with Prometheus-backed series; until that backend
 * exists, the honest answer to "what was CPU doing an hour ago" is that we do
 * not know.
 *
 * So: no seeding, no synthesis, no interpolation. The buffer starts empty and
 * fills one point per poll. `observed` lets panels say how much they are
 * actually showing rather than implying a time range they do not have.
 */
export function useDashboardSeries(summary, range = "6h") {
  const points = RANGE_POINTS[range] || RANGE_POINTS["6h"];
  const clusterId = summary?.clusterId;
  const cpu = summary?.cpuUsage;
  const mem = summary?.memoryUsage;
  const sampledAt = summary?.lastUpdated;

  const bufferRef = useRef({ clusterId: "", cpu: [], mem: [] });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!summary) {
      return;
    }
    const buffer = bufferRef.current;

    // A different cluster's readings are not this cluster's history. Changing
    // range keeps what we have and only changes how much is retained.
    if (buffer.clusterId !== (clusterId || "")) {
      buffer.clusterId = clusterId || "";
      buffer.cpu = [];
      buffer.mem = [];
    }

    const push = (arr, value) => {
      if (value == null) {
        return;
      }
      arr.push(value);
      while (arr.length > points) {
        arr.shift();
      }
    };

    // `available: false` means the metrics source did not answer. Recording a
    // zero for that would be indistinguishable from a genuinely idle cluster.
    push(buffer.cpu, cpu?.available ? Number(cpu.percent) || 0 : null);
    push(buffer.mem, mem?.available ? Number(mem.percent) || 0 : null);

    setTick((value) => value + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId, range, sampledAt, points]);

  return useMemo(() => {
    const buffer = bufferRef.current;
    return {
      cpu: buffer.cpu,
      mem: buffer.mem,
      cpuAvailable: Boolean(cpu?.available),
      memAvailable: Boolean(mem?.available),
      // How many readings this browser has actually seen, so a panel can say
      // "3 readings" instead of implying an hour of history.
      observed: Math.max(buffer.cpu.length, buffer.mem.length),
      capacity: points,
    };
    // Recompute when a sample lands and right after the effect appends, so the
    // first reading paints without waiting for the next poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sampledAt, clusterId, range, points, cpu?.available, mem?.available, tick]);
}
