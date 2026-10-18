import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the API runs on `voiceagent serve --port 8080`; Vite proxies to it so cookies stay
// same-origin. In production FastAPI serves the built files from web/dist.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8080",
      "/voice": "http://localhost:8080",
      "/health": "http://localhost:8080",
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
