// @vitest-environment node
/// <reference types="node" />
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const web = fileURLToPath(new URL("../../", import.meta.url));

describe("dashboard installation metadata", () => {
  it("opens the existing dashboard home in a standalone window", () => {
    const html = readFileSync(`${web}/index.html`, "utf8");
    const manifest = JSON.parse(readFileSync(`${web}/public/manifest.webmanifest`, "utf8"));
    expect(html).toContain('rel="manifest" href="/manifest.webmanifest"');
    expect(html).toContain(`<meta name="theme-color" content="${manifest.theme_color}"`);
    expect(manifest).toMatchObject({
      name: "Looper", short_name: "Looper", start_url: "/", scope: "/", display: "standalone",
    });
    expect(manifest.background_color).toBe(manifest.theme_color);
    expect(manifest.prefer_related_applications).not.toBe(true);
    const icons = manifest.icons as { src: string; sizes: string; type: string; purpose: string }[];
    expect(icons.filter(icon => icon.purpose === "any").map(icon => icon.sizes).sort())
      .toEqual(["192x192", "512x512"]);
    expect(icons.some(icon => icon.purpose === "maskable" && icon.sizes === "512x512")).toBe(true);
    for (const icon of icons) {
      const png = readFileSync(`${web}/public${icon.src}`);
      expect(icon.type).toBe("image/png");
      expect(`${png.readUInt32BE(16)}x${png.readUInt32BE(20)}`).toBe(icon.sizes);
    }
  });
});
