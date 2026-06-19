import { useRef } from "react";
import { useCanvasChart } from "../../hooks/useCanvasChart.js";
import { cssVar, spark } from "./chartDraw.js";

// Compact sparkline for KPI / stat cards. `color` accepts a hex or a CSS var
// name (e.g. "--accent"), which is resolved against the active theme.
export default function Sparkline({ data = [], color = "--accent", height = 26 }) {
  const ref = useRef(null);
  const resolved = color.startsWith("--") ? cssVar(color, "#3b82f6") : color;

  useCanvasChart(
    ref,
    (ctx, { width, height: h }) => {
      if (data.length < 2) return;
      spark(ctx, width, h, data, resolved);
    },
    [data, resolved]
  );

  return <canvas ref={ref} className="sparkline-canvas" style={{ height }} aria-hidden="true" />;
}
