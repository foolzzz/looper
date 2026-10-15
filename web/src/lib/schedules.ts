// What a schedule's row shows the same way wherever it is listed: its project's Schedules tab and the Schedules
// page.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ScheduleNotify, ScheduleRunInfo, ScheduleRunStatus } from "@/lib/api";

/** How long the answer to "Run now" or a note stays beside the row's controls; errors stay. */
const NOTICE_MS = 10_000;

type Variant = "default" | "success" | "warning" | "destructive" | "outline";

/** Each run status's label and badge. */
export const RUN_STATUS: Record<ScheduleRunStatus, { label: string; variant: Variant }> = {
  queued: { label: "Queued", variant: "warning" },
  gating: { label: "Checking", variant: "default" },
  running: { label: "Running", variant: "default" },
  completed: { label: "Completed", variant: "success" },
  failed: { label: "Failed", variant: "destructive" },
  stopped: { label: "Stopped", variant: "warning" },
  cancelled: { label: "Cancelled", variant: "outline" },
  timed_out: { label: "Timed out", variant: "destructive" },
  skipped_overlap: { label: "Skipped: busy", variant: "outline" },
  skipped_missed: { label: "Skipped: missed", variant: "outline" },
  skipped_gate: { label: "Skipped: nothing new", variant: "outline" },
  gate_failed: { label: "Gate failed", variant: "destructive" },
  skipped_budget: { label: "Skipped: daily budget", variant: "warning" },
};

/** What each `notify` choice sends. */
export const NOTIFY_OPTIONS: { value: ScheduleNotify; label: string; hint: string }[] = [
  {
    value: "failures",
    label: "Failures",
    hint: "Failed runs, plus the alerts (3 failures in a row, no success for a while) and their recoveries.",
  },
  { value: "always", label: "Always", hint: "Every run that ran a task, plus the alerts." },
  { value: "never", label: "Never", hint: "Nothing is sent; the dashboard still shows failing schedules." },
];

/** `record` without `key`. */
export function omit<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

/** When a run last fired: a run that counts repeated skips (`firings`) last fired when it was last counted. */
export function lastFiredAt(run: ScheduleRunInfo): string {
  return run.firings > 1 && run.finished_at ? run.finished_at : run.fired_at ?? run.scheduled_for;
}

/** A note beside a schedule's controls. */
export interface Notice {
  text: string;
  tone: "ok" | "warn" | "error";
}

/** What "Run now" answered, in words. */
export function runNotice(run: ScheduleRunInfo): Notice {
  switch (run.status) {
    case "running":
      return { text: "Started", tone: "ok" };
    case "queued":
      return { text: "Queued behind the running task", tone: "ok" };
    case "gating":
      return { text: "Checking: the gate is running", tone: "ok" };
    case "skipped_overlap":
      return { text: "Skipped: the previous run is still in progress", tone: "warn" };
    case "skipped_budget":
      return { text: "Skipped: today's spend reached the daily budget", tone: "warn" };
    case "skipped_gate":
      return { text: "Skipped: the gate printed nothing, so there is nothing new", tone: "warn" };
    case "gate_failed":
      return { text: run.error ? `Gate failed: ${run.error}` : "Gate failed", tone: "error" };
    default:
      return {
        text: run.error ? `Not started: ${run.error}` : RUN_STATUS[run.status]?.label ?? run.status,
        tone: run.status === "failed" || run.status === "timed_out" ? "error" : "warn",
      };
  }
}

/** A message per schedule beside its controls; all but errors go after a while. */
export function useNotices() {
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  useEffect(() => {
    const pending = timers.current;
    return () => Object.values(pending).forEach(clearTimeout);
  }, []);
  const notify = useCallback((scheduleId: string, notice: Notice | null) => {
    clearTimeout(timers.current[scheduleId]);
    setNotices((current) => (notice ? { ...current, [scheduleId]: notice } : omit(current, scheduleId)));
    if (notice && notice.tone !== "error")
      timers.current[scheduleId] = setTimeout(
        () => setNotices((current) => (current[scheduleId] === notice ? omit(current, scheduleId) : current)),
        NOTICE_MS,
      );
  }, []);
  return { notices, notify };
}
