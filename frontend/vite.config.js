import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Relative asset URLs so opening via Flask (/) or file preview does not 404 on /assets/...
  base: "./",
  plugins: [react()],
  test: {
    environment: "node",
  },
  build: {
    target: "es2020",
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
