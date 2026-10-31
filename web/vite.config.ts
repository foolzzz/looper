import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { defineConfig, type Plugin } from "vite";

function sourceFingerprint(): Plugin {
  let outDir: string;
  return {
    name: "looper-source-fingerprint",
    apply: "build",
    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir);
    },
    closeBundle() {
      const root = path.resolve(import.meta.dirname, "..");
      execFileSync(process.env.PYTHON ?? "python3", [
        path.join(root, "looper/components.py"), root, outDir,
      ]);
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), sourceFingerprint()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  base: "./",
  server: {
    proxy: {
      "/api": "http://localhost:8767",
    },
  },
});
