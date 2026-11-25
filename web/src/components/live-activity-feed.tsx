import { useEffect, useRef, useState } from "react";
import { Activity, Loader2 } from "lucide-react";
import { fetchTaskActivity, streamTaskActivity, type ActivityEvent } from "@/lib/api";
import { activityKindIcon, formatTime } from "@/lib/activity";
import { cn } from "@/lib/utils";

export function LiveActivityFeed({ taskId, isActive }: { taskId: string; isActive: boolean }) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [live, setLive] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const { activity } = await fetchTaskActivity(taskId, 0, 200);
        if (cancelled) return;
        setEvents(activity);
      } catch {
        // ignore
      }

      if (!isActive) return;

      const maxSeq = events.length > 0 ? Math.max(...events.map((e) => e.seq)) : 0;
      setLive(true);
      cleanupRef.current = streamTaskActivity(
        taskId,
        maxSeq,
        (event) => {
          if (cancelled) return;
          setEvents((prev) => {
            if (prev.some((e) => e.seq === event.seq)) return prev;
            return [...prev, event];
          });
        },
        () => setLive(false),
      );
    })();

    return () => {
      cancelled = true;
      cleanupRef.current?.();
    };
  }, [taskId, isActive]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [events]);

  if (events.length === 0 && !isActive) return null;

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <Activity className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-xs font-medium">Live Activity</span>
        <span className="text-[10px] text-muted-foreground">{events.length} events</span>
        {live && (
          <span className="flex items-center gap-1 text-xs text-success ml-auto">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-75" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
            </span>
            Streaming
          </span>
        )}
      </div>
      <div ref={scrollRef} className="max-h-[300px] overflow-y-auto p-3 space-y-1">
        {events.length === 0 && (
          <div className="flex items-center justify-center py-4">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground mr-2" />
            <span className="text-xs text-muted-foreground">Waiting for activity...</span>
          </div>
        )}
        {events.map((ev) => (
          <div
            key={ev.seq}
            className={cn(
              "flex items-start gap-2 rounded px-2 py-1.5 text-xs",
              ev.kind === "error" ? "bg-destructive/10" : "hover:bg-muted/50",
            )}
          >
            {activityKindIcon(ev.kind)}
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-medium text-muted-foreground">
                  {ev.worker || "system"}
                </span>
                <span className="text-[10px] text-muted-foreground/60">
                  {formatTime(ev.created_at)}
                </span>
              </div>
              <p className={cn(
                "mt-0.5 whitespace-pre-wrap break-all",
                ev.kind === "error" && "text-destructive",
              )}>
                {ev.summary || ev.kind}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
