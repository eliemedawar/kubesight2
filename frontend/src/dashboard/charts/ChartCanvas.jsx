import { useRef } from "react";
import { useCanvasChart } from "../../hooks/useCanvasChart.js";

// Thin wrapper: a <canvas> bound to a draw function via useCanvasChart. Lets the
// ops dashboard lay out each chart's bespoke header/legend itself while sharing
// the DPR-scaling + redraw machinery.
export default function ChartCanvas({ draw, deps = [], className = "ops-chart-canvas" }) {
  const ref = useRef(null);
  useCanvasChart(ref, draw, deps);
  return <canvas ref={ref} className={className} />;
}
