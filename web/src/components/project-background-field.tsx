import { AlertCircle } from "lucide-react";
import { BACKGROUND_MAX_CHARS } from "@/lib/api";
import { cn } from "@/lib/utils";

/** True when a background is too long to save. */
export function backgroundTooLong(value: string) {
  return value.length > BACKGROUND_MAX_CHARS;
}

/** A project's background: a monospace textarea with a live length counter and what it is used for. */
export function ProjectBackgroundField({
  id,
  value,
  onChange,
  rows = 6,
  labelClassName = "text-xs",
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  labelClassName?: string;
}) {
  const over = backgroundTooLong(value);
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className={cn("font-medium text-muted-foreground", labelClassName)}>
          Background
        </label>
        <span
          id={`${id}-count`}
          className={cn(
            "flex items-center gap-1 text-[11px] tabular-nums",
            over ? "text-destructive" : "text-muted-foreground",
          )}
        >
          {over && <AlertCircle className="h-3 w-3" aria-label="Too long" />}
          {value.length} / {BACKGROUND_MAX_CHARS}
        </span>
      </div>
      <textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        spellCheck={false}
        aria-invalid={over}
        aria-describedby={`${id}-count ${id}-hint`}
        placeholder="Conventions, constraints and context every agent of this project should know"
        className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring aria-invalid:border-destructive"
      />
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        Added to the system prompt of this project's Planner, Workers and Reviewers, and stored in run records: do not
        put secrets in it.
      </p>
    </div>
  );
}
