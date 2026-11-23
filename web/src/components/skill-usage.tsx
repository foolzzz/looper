import { useEffect, useId, useState } from "react";
import { ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  fetchSkillUsage,
  type SkillRate,
  type SkillReviewReason,
  type SkillUsagePeriod,
  type SkillUsageRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";

export type SkillUsageState = { rows: Map<string, SkillUsageRow> | null; error: string };

/** One read of a project's skill usage, keyed by skill id. */
export function useSkillUsage(projectId: string): SkillUsageState {
  const [state, setState] = useState<SkillUsageState>({ rows: null, error: "" });
  useEffect(() => {
    let alive = true;
    fetchSkillUsage(projectId)
      .then((list) => alive && setState({ rows: new Map(list.map((row) => [row.skill_id, row])), error: "" }))
      .catch((err) => alive && setState({ rows: null, error: err instanceof Error ? err.message : String(err) }));
    return () => {
      alive = false;
    };
  }, [projectId]);
  return state;
}

const REVIEW_TEXT: Record<SkillReviewReason, string> = {
  never_invoked: "Loaded by more than 30 attempts and never called",
  stale: "Not called in 90 days of runs that can tell",
  divergent: "First-pass interval differs from attempts without it",
};

const percent = (value: number) => `${Math.round(value * 100)}%`;

function rateText(rate: SkillRate): string {
  if (rate.state === "insufficient" || rate.value == null || rate.low == null || rate.high == null) {
    return `not enough data (${rate.successes} of ${rate.n})`;
  }
  return `${percent(rate.value)} (${rate.successes} of ${rate.n}, 95% ${percent(rate.low)}–${percent(rate.high)})`;
}

function callsText(period: SkillUsagePeriod): string {
  const { count, n, unknown, rate } = period.invoked;
  if (n === 0) return unknown > 0 ? "calls unknown for this runtime" : "no calls recorded";
  const known = `called in ${count} of ${n}${rate != null ? ` (${percent(rate)})` : ""}`;
  return unknown > 0 ? `${known}, ${unknown} unknown` : known;
}

function loadedText(period: SkillUsagePeriod): string {
  if (period.loaded_attempts == null) return `${period.loaded_runs} ${period.loaded_runs === 1 ? "run" : "runs"} loaded it`;
  const word = period.loaded_attempts === 1 ? "attempt" : "attempts";
  return `${period.loaded_attempts} ${word} loaded it`;
}

function firstPassText(period: SkillUsagePeriod): string {
  if (period.effect == null) return "first pass: attempts are not recorded here";
  const { first_pass: first, control } = period.effect;
  if (first.state === "insufficient") {
    return `first pass: not enough data (${first.n} with it, ${control.n} without; 20 needed on each side)`;
  }
  return `first pass ${rateText(first)}`;
}

function money(value: number | null): string {
  return value == null ? "unknown" : `$${value.toFixed(2)}`;
}

function Detail({ row }: { row: SkillUsageRow }) {
  const days = (a: string, b: string) => `${a.slice(0, 10)} to ${b.slice(0, 10)}`;
  const blocks: [string, SkillUsagePeriod, { start: string; end: string }][] = [
    ["Last 7 days", row, row.window],
    ["The 28 days before", row.comparison, row.comparison_window],
  ];
  return (
    <div className="space-y-2 break-words text-xs text-muted-foreground">
      {row.review.length > 0 && (
        <ul className="space-y-0.5 text-foreground">
          {row.review.map((reason) => (
            <li key={reason}>Suggested review: {REVIEW_TEXT[reason]}.</li>
          ))}
        </ul>
      )}
      {blocks.map(([label, period, span]) => (
        <div key={label} className="space-y-0.5">
          <p className="font-medium text-foreground">{label} ({days(span.start, span.end)})</p>
          <p>{loadedText(period)}; {callsText(period)}.</p>
          {period.effect && (
            <>
              <p>
                First pass with it: {rateText(period.effect.first_pass)}. Without it:{" "}
                {rateText(period.effect.control)}.
              </p>
              <p>
                Cost per first-pass attempt: with it {money(period.effect.cost.loaded.avg)}, without it{" "}
                {money(period.effect.cost.control.avg)}.
              </p>
            </>
          )}
        </div>
      ))}
      <p>
        Versions with this content: {row.versions.length ? row.versions.join(", ") : "unknown"}.
        {row.coverage_since ? ` Attempts are recorded since ${row.coverage_since.slice(0, 10)}.` : ""}
      </p>
      <p>{row.note}</p>
    </div>
  );
}

/** The usage line of one project skill, with its control and review reasons behind a toggle. */
export function SkillUsage({ skillId, usage }: { skillId: string; usage: SkillUsageState }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  if (usage.error) {
    return <p role="alert" className="break-words pl-5 text-xs text-destructive">Usage unavailable: {usage.error}</p>;
  }
  if (usage.rows == null) return <p className="pl-5 text-xs text-muted-foreground">Loading usage…</p>;
  const row = usage.rows.get(skillId);
  if (!row) return <p className="pl-5 text-xs text-muted-foreground">Usage: not available for this skill.</p>;
  const idle = row.loaded_runs === 0 && (row.loaded_attempts ?? 0) === 0;
  return (
    <div className="space-y-1 pl-5">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={`${id}-detail`}
        onClick={() => setOpen(!open)}
        className="flex w-full min-w-0 items-start gap-1 rounded-md text-left text-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <ChevronRight
          className={cn("mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform duration-150 motion-reduce:transition-none", open && "rotate-90")}
          aria-hidden="true"
        />
        <span className="min-w-0 break-words">
          <span className="font-medium">Usage, last 7 days:</span>{" "}
          {idle ? (
            <span className="text-muted-foreground">not loaded by any run</span>
          ) : (
            <span className="text-muted-foreground">
              {loadedText(row)}; {callsText(row)}; {firstPassText(row)}
            </span>
          )}
          {row.review.length > 0 && (
            <Badge variant="warning" className="ml-2 py-0 align-middle text-[11px]">Suggested review</Badge>
          )}
        </span>
      </button>
      {open && (
        <div id={`${id}-detail`} className="ml-4">
          <Detail row={row} />
        </div>
      )}
    </div>
  );
}
