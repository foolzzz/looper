import { useEffect, useLayoutEffect, useRef, useState } from "react";
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
  ["", "All statuses"],
  ["init", "Ready"],
  ["running", "Running"],
  ["waiting", "Waiting"],
  ["waiting_user", "Waiting for you"],
  ["awaiting_budget", "等你决定花费"],
  ["pending", "Pending"],
  ["completed", "Completed"],
  ["failed", "Failed"],
  ["cancelled", "Cancelled"],
  ["stopped", "Stopped"],
];

export function WorkList({
  selection,
  preferredProjectId,
  onOpenTask,
  onOpenManager,
  onTasks,
  onFinder,
  onFind,
}: {
  selection: { kind: "manager" } | { kind: "task"; taskId: string };
  preferredProjectId: string | null;
  onOpenTask: (taskId: string) => void;
  onOpenManager: () => void;
  onTasks: (tasks: WorkTask[]) => void;
  onFinder: (available: boolean) => void;
  onFind: () => void;
}) {
  const { projects } = useProjects();
  const open = useWorkSection<WorkTask>("open", true);
  const schedules = useWorkSection<WorkSchedule>("schedules", true);
  const [endedOpen, setEndedOpen] = useState(() => workStorage.endedOpen());
  const [projectFilter, setProjectFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const ended = useWorkSection<WorkTask>("ended", endedOpen, projectFilter, statusFilter);
  const [collapsed, setCollapsed] = useState<string[]>(() => workStorage.collapsed());
  const [drafts, setDrafts] = useState<boolean | null>(null);
  const [finder, setFinder] = useState<boolean | null>(null);
  const [draftError, setDraftError] = useState("");
  const [creating, setCreating] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const restored = useRef(false);

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
  useEffect(() => { onTasks([...open.items, ...ended.items]); }, [open.items, ended.items, onTasks]);
  useEffect(() => {
    if (preferredProjectId) workStorage.setLastProject(preferredProjectId);
  }, [preferredProjectId]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || restored.current || (open.loading && open.items.length === 0)) return;
    el.scrollTop = workStorage.scroll();
    restored.current = true;
  }, [open.loading, open.items.length]);

  const saveScroll = () => {
    if (scroller.current) workStorage.setScroll(scroller.current.scrollTop);
  };
  const projectId = preferredProjectId || workStorage.lastProject() || projects?.[0]?.project_id || "";
  const draftReason = drafts === false
    ? DRAFTS_UNAVAILABLE
    : !projectId && projects !== null
      ? "Create a project before a draft task."
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
        <Button type="button" className="w-full" disabled={drafts === false || !projectId || creating} onClick={() => void onNew()}
          aria-describedby={draftReason ? "work-draft-reason" : undefined}>
          New task
        </Button>
        {draftReason && <p id="work-draft-reason" className="text-xs text-muted-foreground">{draftReason}</p>}
        <Input
          readOnly={finder !== false}
          disabled={finder === false}
          aria-label="Find a task"
          placeholder="Find a task"
          aria-describedby={finder === false ? "work-find-reason" : undefined}
          onFocus={() => { if (finder !== false) onFind(); }}
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
          Manager
        </a>
      </div>
      <div ref={scroller} aria-label="Work tasks" onScroll={saveScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <section aria-label="Open tasks" aria-busy={open.loading} className="border-b border-border pb-2">
          <h2 className="px-3 pt-3 text-xs font-medium text-muted-foreground">Open</h2>
          <SectionState section="open" state={open} empty="No open tasks" />
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
        <section aria-label="Schedules" aria-busy={schedules.loading} className="border-b border-border pb-2">
          <h2 className="px-3 pt-3 text-xs font-medium text-muted-foreground">Schedules</h2>
          <SectionState section="schedules" state={schedules} empty="No schedules" />
          {schedules.items.map((schedule) => (
            <WorkScheduleRow key={schedule.schedule_id} schedule={schedule} onOpenTask={(id) => { saveScroll(); onOpenTask(id); }} />
          ))}
          {schedules.next && <More loading={schedules.loading} onClick={schedules.loadMore} />}
        </section>
        <section aria-label="Ended" aria-busy={ended.loading} className="pb-3">
          <button type="button" aria-expanded={endedOpen} onClick={toggleEnded}
            className={`flex w-full items-center px-3 py-2 text-sm font-medium pointer-coarse:min-h-11 ${FOCUS}`}>
            Ended
          </button>
          {endedOpen && (
            <>
              <div className="grid grid-cols-2 gap-2 px-3 pb-2">
                <label className={LABEL}>Project
                  <select aria-label="Ended project" className={FILTER} value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
                    <option value="">All projects</option>
                    {(projects ?? []).map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
                  </select>
                </label>
                <label className={LABEL}>Status
                  <select aria-label="Ended status" className={FILTER} value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                    {STATUSES.map(([value, label]) => <option key={value || "all"} value={value}>{label}</option>)}
                  </select>
                </label>
              </div>
              <SectionState section="ended" state={ended} empty="No ended tasks" />
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
        <Button type="button" variant="outline" onClick={state.retry}>Retry</Button>
      </div>
    );
  }
  if (state.loading && state.items.length === 0) return <p className="px-3 py-2 text-sm text-muted-foreground">Loading…</p>;
  if (state.items.length === 0) return <p className="px-3 py-2 text-sm text-muted-foreground">{empty}</p>;
  return null;
}

function More({ loading, onClick }: { loading: boolean; onClick: () => void }) {
  return (
    <div className="px-3 py-2">
      <Button type="button" variant="outline" disabled={loading} onClick={onClick}>Load more</Button>
    </div>
  );
}
