import type { ScheduleInfo } from "@/lib/api";

/** A number of seconds as hours, minutes or seconds, whichever is whole. */
export function seconds(value: number): string {
  if (value % 3600 === 0) return `${value / 3600} h`;
  if (value % 60 === 0) return `${value / 60} min`;
  return `${value} s`;
}

/** A schedule's run timeout in words; a schedule that follows the default says what that is now. */
export function runTimeoutText(schedule: Pick<ScheduleInfo, "run_timeout_seconds" | "run_timeout_effective_seconds">) {
  if (schedule.run_timeout_seconds === "default") {
    const effective = schedule.run_timeout_effective_seconds;
    return `The default (${effective == null ? "no limit" : seconds(effective)})`;
  }
  return schedule.run_timeout_seconds == null ? "None" : seconds(schedule.run_timeout_seconds);
}
