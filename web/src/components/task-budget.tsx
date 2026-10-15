import { type FormEvent, useState } from "react";
import { AlertTriangle, Play } from "lucide-react";
import { continueTaskAtCap } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

/** The share of its cap a task may spend before the task page warns that it is near the cap. */
export const NEAR_CAP = 0.8;

/** A task's spend as a share of its project's cap per task, with a warning from NEAR_CAP on. */
export function TaskSpendMeter({ spent, cap }: { spent: number; cap: number }) {
  const share = spent / cap;
  const percent = Math.round(share * 100);
  const reached = share >= 1;
  const near = !reached && share >= NEAR_CAP;
  return (
    <div className="mt-2 space-y-1">
      <div
        className="h-1.5 overflow-hidden rounded-full bg-muted"
        role="meter"
        aria-label="Share of the task spend cap used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.min(percent, 100)}
      >
        <div
          className={cn("h-full rounded-full", reached ? "bg-destructive" : near ? "bg-warning" : "bg-primary/70")}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
      <p className={cn("flex items-center gap-1 text-xs", reached ? "text-destructive" : near ? "text-warning" : "text-muted-foreground")}>
        {(near || reached) && <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />}
        <span>
          {percent}% of {formatUsd(cap)} cap{reached ? " · reached" : near ? " · near the cap" : ""}
        </span>
      </p>
    </div>
  );
}

/** A first guess at the new cap: twice the old one, or half again the spend if that is more. */
function suggestedCap(spent: number, cap: number | null): string {
  const value = Math.max((cap ?? 0) * 2, spent * 1.5, 0.01);
  return String(Math.ceil(value * 100) / 100);
}

/** For a task that failed at its spend cap: raise the cap above the spend and continue from the saved plan. The cap
 * is the project's unless the task has its own (`ownCap`, a scheduled run's). */
export function ContinueAtCap({
  taskId,
  projectName,
  spent,
  cap,
  ownCap,
  onContinued,
}: {
  taskId: string;
  projectName: string;
  spent: number;
  cap: number | null;
  ownCap: boolean;
  onContinued: () => void;
}) {
  const [value, setValue] = useState(() => suggestedCap(spent, cap));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const amount = Number(value);
    if (!value.trim() || !Number.isFinite(amount) || amount <= spent) {
      setError(`Enter more than ${formatUsd(spent)}, what the task has spent.`);
      return;
    }
    setBusy(true);
    setError("");
    continueTaskAtCap(taskId, amount).then(
      () => onContinued(),
      (err) => {
        setBusy(false);
        setError(err instanceof Error ? err.message : String(err));
      },
    );
  };

  return (
    <section aria-labelledby="continue-at-cap" className="rounded-lg border border-warning/40 bg-warning/5 px-4 py-3">
      <h3 id="continue-at-cap" className="flex items-center gap-1.5 text-sm font-semibold text-warning">
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
        Stopped at the spend cap
      </h3>
      <p className="mt-1 text-sm text-muted-foreground">
        The task spent {formatUsd(spent)}
        {cap != null && <> of the {formatUsd(cap)} cap {ownCap ? "for this run" : "per task"}</>}. Raise the cap to
        continue from where it stopped: accepted steps are kept and the Planner keeps its plan.{" "}
        {ownCap ? (
          <>The new cap applies to this task only; its schedule keeps its own.</>
        ) : (
          <>
            The new cap applies to every task in <span className="text-foreground">{projectName}</span>.
          </>
        )}
      </p>
      <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <label htmlFor="continue-at-cap-usd" className="text-xs font-medium text-muted-foreground">
            New cap per task (USD)
          </label>
          <Input
            id="continue-at-cap-usd"
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "continue-at-cap-error" : undefined}
            className="w-36 font-mono"
          />
        </div>
        <Button type="submit" disabled={busy}>
          <Play className="h-3.5 w-3.5" aria-hidden="true" />
          Raise cap and continue
        </Button>
      </form>
      {error && (
        <p id="continue-at-cap-error" role="alert" className="mt-2 text-xs text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}
