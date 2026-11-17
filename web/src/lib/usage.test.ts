import { expect, it } from "vitest";
import { formatTpm } from "@/lib/usage";

it.each([
  [0, "0"], [999, "999"], [1000, "1k"], [12_340, "12.3k"], [12_000, "12k"], [999_949, "999.9k"],
  [999_950, "1M"], [1_200_000, "1.2M"], [15_000_000, "15M"],
])("formats %i tokens per minute as %s", (value, text) => {
  expect(formatTpm(value)).toBe(text);
});
