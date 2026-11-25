import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { WorkList } from "@/components/work-list";
import { WorkSwitcher } from "@/components/work-switcher";
import { fetchTaskDetail, TaskDetailError, type TaskDetail } from "@/lib/api";
import { callV1, V1Error } from "@/lib/v1";
import { errorText } from "@/lib/use-action";
import { PlannerWorkConversation } from "@/components/work-conversation";
import { TaskStatusMark } from "@/pages/tasks";
import { isMissingOperation, markSeen, taskTitle, workSelection, workStorage } from "@/lib/work";

const LINK = "text-sm text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** Centre is the selected task and its Planner conversation. Manager stays a link to Chat. */
export function WorkPage({
  sub,
  onNavigate,
}: {
  sub: string;
  onNavigate: (sub: string, replace?: boolean) => void;
}) {
  const [detail, setDetail] = useState<{ id: string; task?: TaskDetail; error?: string; missing?: boolean }>({ id: "" });
  const [retry, setRetry] = useState(0);
  const [pane, setPane] = useState<"list" | "detail">(sub === "" ? "list" : "detail");
  const [switcher, setSwitcher] = useState(false);
  const [finder, setFinder] = useState(true);
  const opener = useRef<HTMLElement | null>(null);
  const seen = useRef("");
  const restored = useRef(false);
  const onFinder = useCallback((available: boolean) => setFinder(available), []);

  // A bare #work restores the last selection. #work/manager and #work/task/… win over storage.
  useEffect(() => {
    if (sub !== "" || restored.current) return;
    restored.current = true;
    const saved = workStorage.selection();
    if (saved && saved !== "manager") onNavigate(`task/${encodeURIComponent(saved)}`, true);
    else if (saved === "manager") onNavigate("manager", true);
  }, [sub, onNavigate]);

  useEffect(() => { setPane(sub === "" ? "list" : "detail"); }, [sub]);

  const explicit = workSelection(sub);
  const selection = explicit ?? { kind: "manager" as const };
  const taskId = selection.kind === "task" ? selection.taskId : "";
  const selected = detail.id === taskId ? detail.task : undefined;
  useEffect(() => {
    if (!taskId) return;
    const ctrl = new AbortController();
    setDetail({ id: taskId });
    // Read the latest event, not the first forward page (which can be over 1,000 events behind).
    Promise.all([
      fetchTaskDetail(taskId, ctrl.signal),
      callV1<{ activity: { seq: number }[] }>("/api/v1/tasks/activity", { task_id: taskId, limit: 1 }, { signal: ctrl.signal }),
    ]).then(([task, activity]) => {
      if (ctrl.signal.aborted) return;
      if ("status" in activity) throw new Error("Task activity is unavailable");
      setDetail({ id: taskId, task });
      const seq = Math.max(0, ...activity.activity.map(event => event.seq));
      const key = `${taskId}:${seq}`;
      if (seen.current !== key) {
        seen.current = key;
        markSeen(taskId, seq).catch((err: unknown) => console.error(err));
      }
    }).catch((err: unknown) => {
      if (ctrl.signal.aborted) return;
      setDetail({ id: taskId, missing: (err instanceof TaskDetailError || err instanceof V1Error) && err.status === 404 && !isMissingOperation(err),
        error: errorText(err) });
    });
    return () => ctrl.abort();
  }, [taskId, retry]);

  useEffect(() => {
    if (sub === "") return;
    workStorage.setSelection(selection.kind === "manager" ? "manager" : taskId);
  }, [sub, selection.kind, taskId]);

  const openTask = useCallback((id: string) => {
    setPane("detail");
    onNavigate(`task/${encodeURIComponent(id)}`);
  }, [onNavigate]);
  const openManager = useCallback(() => {
    setPane("detail");
    onNavigate("manager");
  }, [onNavigate]);

  const openSwitcher = useCallback(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSwitcher(true);
  }, []);
  const closeSwitcher = useCallback(() => setSwitcher(false), []);
  const restore = useCallback(() => {
    const target = opener.current;
    opener.current = null;
    target?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k" || event.repeat) return;
      if (!finder) return;
      event.preventDefault();
      if (!switcher) openSwitcher();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finder, switcher, openSwitcher]);

  return (
    <div className="flex h-full min-h-0 w-full">
      <div className={`h-full min-h-0 w-full flex-col border-border md:flex md:w-80 md:shrink-0 md:border-r ${pane === "list" ? "flex" : "hidden"}`}>
        <WorkList
          selection={selection}
          preferredProjectId={selected?.project_id ?? null}
          onOpenTask={openTask}
          onOpenManager={openManager}
          onFinder={onFinder}
          onFind={openSwitcher}
        />
      </div>
      <div className={`h-full min-h-0 min-w-0 flex-1 flex-col ${pane === "detail" ? "flex" : "hidden md:flex"}`}>
        <div className="flex h-full min-h-0 flex-col">
          <div className="shrink-0 border-b border-border p-3 md:hidden">
            <Button type="button" variant="outline" onClick={() => setPane("list")}>Back to the list</Button>
          </div>
          {selection.kind === "manager" ? (
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              <header aria-label="Selected task" className="space-y-3">
                <h1 className="text-lg font-semibold">Manager</h1>
                <a className={LINK} href="#chat">Open the Manager chat</a>
              </header>
            </div>
          ) : !selected ? (
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              <header aria-label="Selected task" className="space-y-3">
                {detail.id !== taskId || !detail.error ? <p role="status">Loading task…</p>
                  : detail.missing ? <PlannerWorkConversation taskId={taskId} missing />
                    : <div role="alert"><p>{detail.error}</p><Button variant="outline" onClick={() => setRetry(n => n + 1)}>Retry</Button></div>}
              </header>
            </div>
          ) : (
            <>
              <header aria-label="Selected task" className="shrink-0 space-y-3 border-b border-border p-4">
                <h1 className="text-lg font-semibold">{taskTitle(selected.title)}</h1>
                <TaskStatusMark status={selected.status} />
                {selected.project_name && <p className="text-sm text-muted-foreground">{selected.project_name}</p>}
                <a className={LINK} href={`#tasks/${encodeURIComponent(taskId)}`}>Open the task page</a>
              </header>
              <PlannerWorkConversation key={taskId} taskId={taskId} detail={selected} />
            </>
          )}
        </div>
      </div>
      <WorkSwitcher open={switcher && finder} onClose={closeSwitcher} onOpen={openTask} restore={restore} />
    </div>
  );
}
