/** Dollars and token counts, as the dashboard shows them. */

import type { Compaction, TaskCost } from "@/lib/api";

export function formatUsd(value: number | null | undefined): string {
  if (value == null) return "–";
  return `$${value.toFixed(value !== 0 && Math.abs(value) < 1 ? 4 : 2)}`;
}

/** 950, 18.7k, 1.25M. */
export function formatTokens(value: number | null | undefined): string {
  if (value == null) return "–";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1).replace(/\.?0+$/, "")}k`;
  return `${(value / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
}

/** "25.1k → 1.4k tokens": a compaction's context before and after, as Claude Code counts them. */
export function compactionChange(c: Compaction): string {
  return `${formatTokens(c.pre_tokens)} → ${formatTokens(c.post_tokens)} tokens`;
}

/** What a task's total does not hold yet: runs still going, and runs that ended without a reported cost. */
export function costCaveats(cost: TaskCost): string[] {
  return [
    cost.running ? `${cost.running} still running` : "",
    cost.unpriced_runs ? `${cost.unpriced_runs} ended without a reported cost` : "",
  ].filter(Boolean);
}
