import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function manualChunk(id) {
  if (!id.includes("node_modules")) {
    return undefined;
  }

  if (id.includes("react-dom")) {
    return "vendor-react-dom";
  }

  if (
    id.includes("/react/") ||
    id.includes("\\react\\") ||
    id.includes("/scheduler/") ||
    id.includes("\\scheduler\\")
  ) {
    return "vendor-react";
  }

  return "vendor";
}

export default defineConfig({
  // Absolute asset URLs. Relative ones ("./") resolve against the current
  // directory, which is fine at "/" but breaks the moment a real route has
  // depth: at /fleet/clusters the browser would ask for /fleet/assets/index.js.
  // Flask serves /assets/<path> explicitly (backend/api/frontend_static.py:29),
  // so absolute paths are what that route already expects.
  //
  // Trade-off: opening dist/index.html over file:// no longer resolves assets.
  // Bookmarkable URLs are a requirement; file:// preview of an SPA that needs a
  // live API was not.
  base: "/",
  plugins: [react()],
  test: {
    environment: "node",
  },
  build: {
    target: "es2020",
    rollupOptions: {
      output: {
        manualChunks: manualChunk,
      },
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:5000",
      "/health": "http://127.0.0.1:5000",
    },
  },
});
