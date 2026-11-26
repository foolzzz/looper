import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LABEL } from "@/components/registry-ui";
import { useProjects } from "@/lib/use-projects";
import { isPlainClick, WorkScheduleRow, WorkTaskLink } from "@/components/work-row";
import {
  createDraft,
  DRAFTS_UNAVAILABLE,
  FIND_UNAVAILABLE,
  groupByProject,
  isMissingOperation,
  operationExists,
  unavailableCopy,
  useWorkSection,
  workStorage,
  type WorkSchedule,
  type WorkTask,
} from "@/lib/work";
import { errorText } from "@/lib/use-action";

const FOCUS = "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
const FILTER = `h-9 w-full pointer-coarse:h-11 rounded-md border border-input bg-transparent px-2 text-sm text-foreground ${FOCUS}`;

const STATUSES: [string, string][] = [
  ["", "全部状态"],
  ["init", "待开始"],
  ["running", "运行中"],
  ["waiting", "等待中"],
  ["waiting_user", "等你回复"],
  ["awaiting_budget", "等你决定花费"],
  ["pending", "排队中"],
  ["completed", "已完成"],
  ["failed", "失败"],
  ["cancelled", "已取消"],
  ["stopped", "已停止"],
];

export function WorkList({
  selection,
  preferredProjectId,
  onOpenTask,
  onOpenManager,
  onFinder,
  onFind,
}: {
  selection: { kind: "manager" } | { kind: "task"; taskId: string };
  preferredProjectId: string | null;
  onOpenTask: (taskId: string) => void;
  onOpenManager: () => void;
  onFinder: (available: boolean) => void;
  onFind: () => void;
}) {
  const { projects } = useProjects();
  const open = useWorkSection<WorkTask>("open", true);
  const schedules = useWorkSection<WorkSchedule>("schedules", true);
  const [endedOpen, setEndedOpen] = useState(() => workStorage.endedOpen());
  const [projectFilter, setProjectFilter] = useState(() => workStorage.filters().project);
  const [statusFilter, setStatusFilter] = useState(() => workStorage.filters().status);
  const ended = useWorkSection<WorkTask>("ended", endedOpen, projectFilter, statusFilter);
  const [collapsed, setCollapsed] = useState<string[]>(() => workStorage.collapsed());
  const [drafts, setDrafts] = useState<boolean | null>(null);
  const [finder, setFinder] = useState<boolean | null>(null);
  const [draftError, setDraftError] = useState("");
  const [creating, setCreating] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const newButton = useRef<HTMLButtonElement>(null);
  const restored = useRef(false);
  const scrollTarget = useRef(workStorage.scroll());
  const [restoredSchedules, setRestoredSchedules] = useState<string[]>([]);
  const scheduleRestored = useCallback((id: string) => {
    setRestoredSchedules(ids => ids.includes(id) ? ids : [...ids, id]);
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    let live = true;
    operationExists("/api/v1/tasks/create_draft", ctrl.signal).then(
      (ok) => { if (live) setDrafts(ok); },
      () => { if (live) setDrafts(null); },
    );
    operationExists("/api/v1/tasks/select", ctrl.signal).then(
      (ok) => { if (live) setFinder(ok); },
      () => { if (live) setFinder(null); },
    );
    return () => { live = false; ctrl.abort(); };
  }, []);

  useEffect(() => { onFinder(finder !== false); }, [finder, onFinder]);
  useEffect(() => { workStorage.setFilters(projectFilter, statusFilter); }, [projectFilter, statusFilter]);
  useEffect(() => {
    if (preferredProjectId) workStorage.setLastProject(preferredProjectId);
  }, [preferredProjectId]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || restored.current || !open.ready || !schedules.ready || (endedOpen && !ended.ready)) return;
    if (schedules.items.some(schedule => workStorage.expanded().includes(schedule.schedule_id) && !restoredSchedules.includes(schedule.schedule_id))) return;
    const restore = () => {
      // A selected task hides the list on narrow screens. Wait for Back to give it layout.
      if (restored.current || el.clientHeight === 0) return;
      el.scrollTop = scrollTarget.current;
      restored.current = true;
    };
    restore();
    const observer = new ResizeObserver(restore);
    observer.observe(el);
    return () => observer.disconnect();
  }, [open.ready, schedules.ready, schedules.items, endedOpen, ended.ready, restoredSchedules]);

  const saveScroll = () => {
    if (scroller.current && scroller.current.clientHeight > 0 && restored.current) workStorage.setScroll(scroller.current.scrollTop);
  };
  const projectId = preferredProjectId || workStorage.lastProject() || projects?.[0]?.project_id || "";
  const draftReason = drafts === false
    ? DRAFTS_UNAVAILABLE
    : !projectId && projects !== null
      ? "请先创建项目，再新建草稿任务。"
      : draftError;

  const onNew = async () => {
    const id = preferredProjectId || workStorage.lastProject() || projects?.[0]?.project_id || "";
    if (!id || drafts === false || creating) return;
    setCreating(true);
    setDraftError("");
    try {
      const draft = await createDraft(id);
      workStorage.setLastProject(draft.project_id || id);
      onOpenTask(draft.task_id);
      open.retry();
    } catch (err) {
      if (isMissingOperation(err)) setDrafts(false);
      else setDraftError(errorText(err));
    } finally {
      setCreating(false);
    }
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.repeat || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.key.toLowerCase() !== "n") return;
      const target = document.activeElement;
      if (target instanceof HTMLElement && (target.isContentEditable || target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]'))) return;
      if (document.querySelector('[role="dialog"]') || newButton.current?.disabled) return;
      event.preventDefault();
      newButton.current?.click();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggleGroup = (id: string) => {
    const next = collapsed.includes(id) ? collapsed.filter((item) => item !== id) : [...collapsed, id];
    setCollapsed(next);
    workStorage.setCollapsed(next);
  };
  const toggleEnded = () => {
    const next = !endedOpen;
    setEndedOpen(next);
    workStorage.setEndedOpen(next);
  };
  const openTask = (task: WorkTask) => {
    saveScroll();
    if (task.project_id) workStorage.setLastProject(task.project_id);
    onOpenTask(task.task_id);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-2 border-b border-border p-3">
        <h1 className="text-lg font-semibold">工作台</h1>
        <Button ref={newButton} type="button" className="w-full" disabled={drafts === false || !projectId || creating} onClick={() => void onNew()}
          aria-describedby={draftReason ? "work-draft-reason" : undefined}>
          新建任务
        </Button>
        {draftReason && <p id="work-draft-reason" className="text-xs text-muted-foreground">{draftReason}</p>}
        <Input
          readOnly={finder !== false}
          disabled={finder === false}
          aria-label="查找任务"
          placeholder="查找任务"
          aria-describedby={finder === false ? "work-find-reason" : undefined}
          onKeyDown={(event) => {
            if (finder !== false && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onFind(); }
          }}
          onClick={() => { if (finder !== false) onFind(); }}
        />
        {finder === false && <p id="work-find-reason" className="text-xs text-muted-foreground">{FIND_UNAVAILABLE}</p>}
        <a
          href="#work/manager"
          aria-current={selection.kind === "manager" ? "page" : undefined}
          onClick={(event) => {
            if (!isPlainClick(event)) return;
            event.preventDefault();
            saveScroll();
            onOpenManager();
          }}
          className={`flex items-center rounded-md px-3 py-2 text-sm font-medium pointer-coarse:min-h-11 ${FOCUS} ${selection.kind === "manager" ? "bg-primary/10 text-primary" : "hover:bg-accent"}`}
        >
          管家
        </a>
      </div>
      <div ref={scroller} aria-label="工作台任务" onScroll={saveScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <section aria-label="进行中任务" aria-busy={open.loading} className="border-b border-border pb-2">
          <h2 className="px-3 pt-3 text-xs font-medium text-muted-foreground">进行中</h2>
          <SectionState section="open" state={open} empty="暂无进行中任务" />
          {groupByProject(open.items).map((group) => {
            const shut = collapsed.includes(group.id);
            return (
              <div key={group.id || "none"}>
                <button type="button" aria-expanded={!shut} onClick={() => toggleGroup(group.id)}
                  className={`flex w-full items-center gap-2 px-3 py-2 text-sm pointer-coarse:min-h-11 ${FOCUS}`}>
                  <span className="min-w-0 flex-1 truncate text-left font-medium">{group.name}</span>
                  <span className="tabular-nums text-muted-foreground">{group.tasks.length}</span>
                </button>
                {!shut && group.tasks.map((task) => (
                  <WorkTaskLink key={task.task_id} task={task} current={selection.kind === "task" && selection.taskId === task.task_id} onActivate={() => openTask(task)} />
                ))}
              </div>
            );
          })}
          {open.next && <More loading={open.loading} onClick={open.loadMore} />}
        </section>
        <section aria-label="定时任务" aria-busy={schedules.loading} className="border-b border-border pb-2">
          <h2 className="px-3 pt-3 text-xs font-medium text-muted-foreground">定时任务</h2>
          <SectionState section="schedules" state={schedules} empty="暂无定时任务" />
          {schedules.items.map((schedule) => (
            <WorkScheduleRow key={schedule.schedule_id} schedule={schedule} onRestored={scheduleRestored} onOpenTask={(id) => { saveScroll(); onOpenTask(id); }} />
          ))}
          {schedules.next && <More loading={schedules.loading} onClick={schedules.loadMore} />}
        </section>
        <section aria-label="已结束" aria-busy={ended.loading} className="pb-3">
          <button type="button" aria-expanded={endedOpen} onClick={toggleEnded}
            className={`flex w-full items-center px-3 py-2 text-sm font-medium pointer-coarse:min-h-11 ${FOCUS}`}>
            已结束
          </button>
          {endedOpen && (
            <>
              <div className="grid grid-cols-2 gap-2 px-3 pb-2">
                <label className={LABEL}>项目
                  <select aria-label="已结束任务的项目" className={FILTER} value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
                    <option value="">全部项目</option>
                    {(projects ?? []).map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
                  </select>
                </label>
                <label className={LABEL}>状态
                  <select aria-label="已结束任务的状态" className={FILTER} value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                    {STATUSES.map(([value, label]) => <option key={value || "all"} value={value}>{label}</option>)}
                  </select>
                </label>
              </div>
              <SectionState section="ended" state={ended} empty="暂无已结束任务" />
              {ended.items.map((task) => (
                <WorkTaskLink key={task.task_id} task={task} current={selection.kind === "task" && selection.taskId === task.task_id} onActivate={() => openTask(task)} />
              ))}
              {ended.next && <More loading={ended.loading} onClick={ended.loadMore} />}
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function SectionState<T>({
  section,
  state,
  empty,
}: {
  section: "open" | "ended" | "schedules";
  state: { items: T[]; error: string | null; missing: boolean; loading: boolean; retry: () => void };
  empty: string;
}) {
  if (state.missing) return <p role="status" className="px-3 py-2 text-sm text-muted-foreground">{unavailableCopy(section)}</p>;
  if (state.error) {
    return (
      <div role="alert" className="space-y-2 px-3 py-2">
        <p className="text-sm text-destructive">{state.error}</p>
        <Button type="button" variant="outline" onClick={state.retry}>重试</Button>
      </div>
    );
  }
  if (state.loading && state.items.length === 0) return <p className="px-3 py-2 text-sm text-muted-foreground">正在加载…</p>;
  if (state.items.length === 0) return <p className="px-3 py-2 text-sm text-muted-foreground">{empty}</p>;
  return null;
}

function More({ loading, onClick }: { loading: boolean; onClick: () => void }) {
  return (
    <div className="px-3 py-2">
      <Button type="button" variant="outline" disabled={loading} onClick={onClick}>加载更多</Button>
    </div>
  );
}
