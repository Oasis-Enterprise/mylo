import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite dev server runs on 5173; in dev we proxy /api/* to the Python
// server running on 8099 so SSE and JSON endpoints work from the React
// app without CORS fuss.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8099",
        changeOrigin: true,
        ws: false,
      },
    },
  },
  build: {
    outDir: "../src/mylo/server/static",
    emptyOutDir: true,
  },
});
