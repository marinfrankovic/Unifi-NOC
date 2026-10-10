import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const base = (process.env.BASE_PATH || "/unifi").replace(/\/$/, "") + "/";

export default defineConfig({
  base,
  plugins: [react()],
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1500,
    rolldownOptions: { output: { codeSplitting: { groups: [{ name: "echarts", test: /node_modules[\\/]echarts/ }] } } },
  },
  server: { proxy: { [base + "api"]: { target: "http://localhost:8000", rewrite: (p) => p.replace(base, "/") } } },
});
