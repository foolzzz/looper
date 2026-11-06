export const CACHE_HIT_TITLE = "cache_read / (input + cache_read + cache_write)";

export function cacheHitTitle(reported: number, missing = 0): string {
  if (reported === 0) return "this runtime does not report cache usage";
  return missing > 0
    ? `${CACHE_HIT_TITLE} · partial: ${reported} of ${reported + missing} runs report cache usage`
    : CACHE_HIT_TITLE;
}

export function cacheHit(input: number | null | undefined, read: number | null | undefined,
                         write: number | null | undefined): number | null {
  if (input == null || read == null || write == null) return null;
  const total = input + read + write;
  return total > 0 ? read / total : null;
}

export function formatHit(r: number | null): string {
  return r === null ? "–" : `${Math.round(r * 100)}%`;
}
