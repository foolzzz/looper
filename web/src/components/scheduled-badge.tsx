import { CalendarClock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { TaskInfo } from "@/lib/api";

/** "Scheduled" on a task a schedule created: a link to that schedule's row on its project's Schedules tab. Clicking
 * it does not also open the row it sits in. */
export function ScheduledBadge({ task }: { task: Pick<TaskInfo, "project_id" | "schedule_id" | "schedule_name"> }) {
  if (!task.schedule_id) return null;
  const name = task.schedule_name || task.schedule_id;
  return (
    <a
      href={`#projects/${task.project_id}/schedules/${task.schedule_id}`}
      onClick={(e) => e.stopPropagation()}
      title={`Created by the schedule “${name}”`}
      aria-label={`Scheduled by ${name}`}
      className="inline-flex shrink-0 rounded-md focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      <Badge variant="outline" className="gap-1 hover:text-foreground">
        <CalendarClock className="h-3 w-3" aria-hidden="true" />
        Scheduled
      </Badge>
    </a>
  );
}
