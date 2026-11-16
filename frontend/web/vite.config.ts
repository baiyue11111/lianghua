import {fileURLToPath} from "node:url"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// https://vite.dev/config/
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 3000,
    host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@display": fileURLToPath(new URL("../display/src", import.meta.url)),
    },
  },
  build: {
    rollupOptions: {
      output: {
        // One entry file made every page pay for the chart library and made a
        // business-code change invalidate all of it. Separate the slow-moving
        // vendor code so it can be cached on its own.
        manualChunks: {
          react: ["react", "react-dom"],
          charts: ["echarts", "echarts-for-react"],
        },
      },
    },
  },
});
