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
  plugins: [react(), tailwindcss(), sourceFingerprint(), ...(process.env.VITEST ? [{
    name: "work-main-baseline",
    resolveId(id: string) {
      return id === "virtual:work-main-app" || id === "virtual:work-main-sidebar" ? `${id}.tsx` : undefined;
    },
    load(id: string) {
      if (!id.startsWith("virtual:work-main-")) return;
      const file = id === "virtual:work-main-app.tsx" ? "App.tsx" : "components/sidebar.tsx";
      const source = execFileSync("git", ["show", `origin/main:web/src/${file}`], {
        cwd: path.resolve(import.meta.dirname, ".."), encoding: "utf8",
      });
      return file === "App.tsx"
        ? source.replace('from "@/components/sidebar"', 'from "virtual:work-main-sidebar"') + "\nexport const baselinePages = [...PAGES];\n"
        : source;
    },
  } satisfies Plugin] : [])],
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
