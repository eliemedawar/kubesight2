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
  // Relative asset URLs so opening via Flask (/) or file preview does not 404 on /assets/...
  base: "./",
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
