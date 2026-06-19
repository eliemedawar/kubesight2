import { useEffect, useRef } from "react";

// useCanvasChart wires a <canvas> ref to a pure draw function.
//
//   const ref = useRef(null);
//   useCanvasChart(ref, (ctx, { width, height }) => drawArea(ctx, width, height, data, ...), [data]);
//
// It owns the parts that are easy to get wrong and identical for every chart:
//  - devicePixelRatio scaling so lines stay crisp on HiDPI displays
//  - clearing the surface before each draw
//  - redrawing when `deps` change (new data) AND when the element resizes
//
// `draw` receives a context already scaled to CSS pixels, plus the CSS-pixel
// width/height, so draw functions never deal with the raw backing-store size.
export function useCanvasChart(ref, draw, deps = []) {
  // Keep the latest draw closure without making it a redraw trigger itself.
  const drawRef = useRef(draw);
  drawRef.current = draw;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return undefined;

    const render = () => {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      drawRef.current(ctx, { width: rect.width, height: rect.height });
    };

    render();

    const observer = new ResizeObserver(render);
    observer.observe(canvas);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
