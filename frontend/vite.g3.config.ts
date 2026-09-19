import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Isolated G3 acceptance server. It keeps the production development proxy
// untouched while allowing the UI fixture to use its own SQLite-backed API.
export default defineConfig({
  cacheDir: "../test_output/g3/vite-cache",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5174,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:18000",
        changeOrigin: true,
      },
      "/static": {
        target: "http://127.0.0.1:18000",
        changeOrigin: true,
      },
    },
  },
});
