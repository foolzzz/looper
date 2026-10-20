import type { TaskWait } from "@/lib/api";

/** A wait's badge. Failed is destructive; a gate never is, so this is not a GateLook. */
export interface WaitLook {
  label: string;
  variant: "default" | "outline" | "success" | "warning" | "destructive";
  tone: string;
}

export const WAIT_STATES: Record<TaskWait["state"], WaitLook> = {
  pending: { label: "Not open yet", variant: "outline", tone: "text-muted-foreground" },
  waiting: { label: "Waiting", variant: "outline", tone: "text-muted-foreground" },
  ready: { label: "Ready", variant: "success", tone: "text-success" },
  timed_out: { label: "Timed out", variant: "warning", tone: "text-warning" },
  cancel_requested: { label: "Cancellation requested", variant: "outline", tone: "text-muted-foreground" },
  done: { label: "Done", variant: "success", tone: "text-success" },
  failed: { label: "Failed", variant: "destructive", tone: "text-destructive" },
  skipped: { label: "Skipped", variant: "outline", tone: "text-muted-foreground" },
};

export const waitAnchor = (step: string) => `wait-${step}`;

export function waitCheckText(check: TaskWait["check"]): string {
  if (!check) return "Time only";
  if (check.type === "command") return `Command \`${check.command}\``;
  const ready = { head_changed: "head changed", checks_finished: "checks finished", new_comments: "new comments" };
  return `GitHub PR ${check.repo}#${check.number} · ${ready[check.ready_when]}`;
}

export function waitDuration(seconds: number): string {
  for (const [unit, size] of [["d", 86400], ["h", 3600], ["min", 60]] as const) {
    if (seconds >= size && seconds % size === 0) return `${seconds / size} ${unit}`;
  }
  return `${seconds} s`;
}
