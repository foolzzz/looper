import { useState } from "react";
import { AlertCircle, ChevronDown, ChevronRight } from "lucide-react";
import { HARNESS_MAX_CHARS } from "@/lib/api";
import { cn } from "@/lib/utils";

/** True when a harness prompt is too long to save. */
export function harnessTooLong(value: string) {
  return value.length > HARNESS_MAX_CHARS;
}

/**
 * A harness prompt: a monospace textarea with a live length counter. When `inherits` is given the field
 * is an override, and an empty one says what it inherits.
 */
export function HarnessField({
  id,
  label,
  value,
  onChange,
  inherits,
  hint,
  rows = 6,
  labelClassName = "text-xs",
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  /** What an empty override inherits, e.g. "the default harness (Settings)". */
  inherits?: string;
  hint?: React.ReactNode;
  rows?: number;
  labelClassName?: string;
}) {
  const over = harnessTooLong(value);
  const inheriting = inherits !== undefined && !value.trim();
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className={cn("font-medium text-muted-foreground", labelClassName)}>
          {label}
        </label>
        <span
          id={`${id}-count`}
          className={cn(
            "flex items-center gap-1 text-[11px] tabular-nums",
            over ? "text-destructive" : "text-muted-foreground",
          )}
        >
          {over && <AlertCircle className="h-3 w-3" aria-label="Too long" />}
          {inheriting ? `Inherits ${inherits}` : `${value.length} / ${HARNESS_MAX_CHARS}`}
        </span>
      </div>
      <textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        spellCheck={false}
        aria-invalid={over}
        aria-describedby={`${id}-count${hint ? ` ${id}-hint` : ""}`}
        placeholder={inherits !== undefined ? `Empty: inherits ${inherits}` : undefined}
        className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring aria-invalid:border-destructive"
      />
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
}

/** A project's harness overrides: its Planner's, and its Workers' (which its Reviewers follow), in a collapsible section. */
export function ProjectHarnessOverrides({
  idPrefix,
  planner,
  worker,
  onPlannerChange,
  onWorkerChange,
  labelClassName,
}: {
  idPrefix: string;
  planner: string;
  worker: string;
  onPlannerChange: (value: string) => void;
  onWorkerChange: (value: string) => void;
  labelClassName?: string;
}) {
  const overridden = [planner.trim() && "Planner", worker.trim() && "Worker"].filter(Boolean);
  // Open at first when an override is set; then only the user opens or closes it.
  const [open, setOpen] = useState(overridden.length > 0);
  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={`${idPrefix}-harness-overrides`}
        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        <span>Harness overrides</span>
        <span className="ml-auto font-normal">
          {overridden.length ? `${overridden.join(" and ")} overridden` : "Both inherit the default"}
        </span>
      </button>
      {open && (
        <div id={`${idPrefix}-harness-overrides`} className="space-y-3 border-t border-border px-3 py-3">
          <p className="text-xs text-muted-foreground">
            The harness is an agent&apos;s working principles, boundaries and tone. Empty fields use the default
            harness from Settings.
          </p>
          <HarnessField
            id={`${idPrefix}-planner-harness`}
            label="Planner harness"
            value={planner}
            onChange={onPlannerChange}
            inherits="the default"
            rows={4}
            labelClassName={labelClassName}
          />
          <HarnessField
            id={`${idPrefix}-worker-harness`}
            label="Worker harness"
            value={worker}
            onChange={onWorkerChange}
            inherits="the default"
            hint="Reviewers of this project use it too."
            rows={4}
            labelClassName={labelClassName}
          />
        </div>
      )}
    </div>
  );
}
