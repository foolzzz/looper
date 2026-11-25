import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { WorkList } from "@/components/work-list";
import { WorkSwitcher } from "@/components/work-switcher";
import { TaskStatusMark } from "@/pages/tasks";
import { markSeen, taskTitle, workSelection, workStorage, type WorkTask } from "@/lib/work";

const LINK = "text-sm text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** Centre is a header and links. The conversation and the task panel are later pages. */
export function WorkPage({
  sub,
  onNavigate,
}: {
  sub: string;
  onNavigate: (sub: string, replace?: boolean) => void;
}) {
  const [known, setKnown] = useState<WorkTask[]>([]);
  const [pane, setPane] = useState<"list" | "detail">(sub === "" ? "list" : "detail");
  const [switcher, setSwitcher] = useState(false);
  const [finder, setFinder] = useState(true);
  const opener = useRef<HTMLElement | null>(null);
  const seen = useRef("");
  const restored = useRef(false);
  const onTasks = useCallback((tasks: WorkTask[]) => setKnown(tasks), []);
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
  const selected = taskId ? known.find((task) => task.task_id === taskId) ?? null : null;

  useEffect(() => {
    if (sub === "") return;
    workStorage.setSelection(selection.kind === "manager" ? "manager" : taskId);
  }, [sub, selection.kind, taskId]);

  useEffect(() => {
    if (!selected || selected.last_seq == null) return;
    const key = `${selected.task_id}:${selected.last_seq}`;
    if (seen.current === key) return;
    seen.current = key;
    markSeen(selected.task_id, selected.last_seq).catch((err: unknown) => { console.error(err); });
  }, [selected]);

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
    // The search field opens the switcher on focus. Putting focus back there would open it again.
    if (!target || target.getAttribute("aria-label") === "Find a task") return;
    target.focus();
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

  const running = !!selected && (selected.group === "in_progress" || selected.group === "needs_you");

  return (
    <div className="flex h-full min-h-0 w-full">
      <div className={`h-full min-h-0 w-full flex-col border-border md:flex md:w-80 md:shrink-0 md:border-r ${pane === "list" ? "flex" : "hidden"}`}>
        <WorkList
          selection={selection}
          preferredProjectId={selected?.project_id ?? null}
          onOpenTask={openTask}
          onOpenManager={openManager}
          onTasks={onTasks}
          onFinder={onFinder}
          onFind={openSwitcher}
        />
      </div>
      <div className={`h-full min-h-0 min-w-0 flex-1 flex-col ${pane === "detail" ? "flex" : "hidden md:flex"}`}>
        <div className="flex h-full min-h-0 flex-col">
          <div className="shrink-0 border-b border-border p-3 md:hidden">
            <Button type="button" variant="outline" onClick={() => setPane("list")}>Back to the list</Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            <header aria-label="Selected task" className="space-y-3">
              {selection.kind === "manager" ? (
                <>
                  <h1 className="text-lg font-semibold">Manager</h1>
                  <a className={LINK} href="#chat">Open the Manager chat</a>
                </>
              ) : (
                <>
                  <h1 className="text-lg font-semibold">{taskTitle(selected?.title)}</h1>
                  {selected && <TaskStatusMark status={selected.status} />}
                  {selected?.project_name && <p className="text-sm text-muted-foreground">{selected.project_name}</p>}
                  <div className="flex flex-wrap gap-4">
                    <a className={LINK} href={`#tasks/${encodeURIComponent(taskId)}`}>Open the task page</a>
                    {running && (
                      <a className={LINK} href={`#chat?${new URLSearchParams({ target: `planner-${taskId}` })}`}>Talk to its Planner</a>
                    )}
                  </div>
                </>
              )}
            </header>
          </div>
        </div>
      </div>
      <WorkSwitcher open={switcher && finder} onClose={closeSwitcher} onOpen={openTask} restore={restore} />
    </div>
  );
}
