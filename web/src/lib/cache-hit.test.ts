import { describe, expect, it } from "vitest";
import { cacheHit, formatHit } from "@/lib/cache-hit";

describe("cache hit", () => {
  it.each([[null, 1, 2], [1, null, 2], [1, 2, null], [undefined, 1, 2]])(
    "keeps incomplete token counts unknown: %s / %s / %s", (input, read, write) => {
      expect(cacheHit(input, read, write)).toBeNull();
    });
  it("omits an empty prompt", () => {
    expect(cacheHit(0, 0, 0)).toBeNull();
    expect(formatHit(null)).toBe("–");
  });
  it.each([[10, 80, 10, .8, "80%"], [100, 0, 0, 0, "0%"], [0, 2, 1, 2 / 3, "67%"]])(
    "uses all prompt tokens and rounds the percentage", (input, read, write, ratio, label) => {
      expect(cacheHit(input, read, write)).toBe(ratio);
      expect(formatHit(ratio)).toBe(label);
    });
});
