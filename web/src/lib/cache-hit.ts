export const CACHE_HIT_TITLE = "cache_read / (input + cache_read + cache_write)";

export function cacheHit(input: number | null | undefined, read: number | null | undefined,
                         write: number | null | undefined): number | null {
  if (input == null || read == null || write == null) return null;
  const total = input + read + write;
  return total > 0 ? read / total : null;
}

export function formatHit(r: number | null): string {
  return r === null ? "–" : `${Math.round(r * 100)}%`;
}
