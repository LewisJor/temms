import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/ui/assets/hub/",
  build: {
    outDir: "../src/temms/ui/static/hub",
    emptyOutDir: true,
    sourcemap: false,
    manifest: true,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]"
      }
    }
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/v1": "http://127.0.0.1:8080"
    }
  }
});
