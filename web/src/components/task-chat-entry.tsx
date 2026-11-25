import { useEffect, useId, useState } from "react";
import { MessageSquare } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import { fetchChatTargets, type ChatTargetInfo } from "@/lib/api";
import { startPolling } from "@/lib/polling";

type Availability = { targets: ChatTargetInfo[]; reason?: string };
const CHECKING: Availability = { targets: [], reason: "Checking chat availability" };

/** One read per page, shared by its rows. The server's live Planners define availability, not task status. */
export function useTaskChatTargets(enabled = true): Availability {
  const [availability, setAvailability] = useState<Availability>(CHECKING);
  useEffect(() => {
    setAvailability(CHECKING);
    if (!enabled) return;
    return startPolling(async signal => {
      try {
        const targets = await fetchChatTargets();
        if (!signal.aborted) setAvailability({ targets });
      } catch {
        if (!signal.aborted) setAvailability({ targets: [], reason: "Chat availability could not be checked" });
      }
    }, 10_000);
  }, [enabled]);
  return availability;
}

export function TaskChatEntry({ taskId, availability }: { taskId: string; availability: Availability }) {
  const reasonId = useId();
  const planner = availability.targets.find(t => t.kind === "planner" && t.task_id === taskId && t.can_send);
  const reason = availability.reason ?? "Only tasks with a running Planner can be chatted with";
  const className = buttonVariants({ variant: "outline", size: "sm", className: "shrink-0 gap-1.5" });
  const label = <><MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />Talk to this task</>;

  return planner ? (
    <a
      href={`#chat?${new URLSearchParams({ target: planner.id })}`}
      onClick={event => event.stopPropagation()}
      className={`${className} text-primary hover:bg-accent`}
    >
      {label}
    </a>
  ) : (
    <span className="inline-flex max-w-64 flex-col items-start gap-1 max-sm:w-full max-sm:max-w-none" onClick={event => event.stopPropagation()}>
      <span
        role="link"
        aria-label="Talk to this task"
        aria-disabled="true"
        aria-describedby={reasonId}
        tabIndex={0}
        title={reason}
        className={`${className} cursor-not-allowed text-muted-foreground opacity-50`}
      >
        {label}
      </span>
      <span id={reasonId} className="text-xs text-muted-foreground">{reason}</span>
    </span>
  );
}
