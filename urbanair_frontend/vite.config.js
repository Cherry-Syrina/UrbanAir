import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev-only proxy so the frontend can call "/api/*" without CORS.
// Override the backend target with VITE_DEV_PROXY_TARGET if needed.
const DEV_PROXY_TARGET = process.env.VITE_DEV_PROXY_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: DEV_PROXY_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
