import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ManagerWorkConversation, PlannerWorkConversation, TaskWorkConversation } from "@/components/work-conversation";
import { WorkAttention } from "@/components/work-attention";
import { WorkList } from "@/components/work-list";
import { WorkSwitcher } from "@/components/work-switcher";
import { WorkChanges } from "@/components/work-changes";
import { WorkGates } from "@/components/work-gates";
import { WorkRequirement } from "@/components/work-requirement";
import { WorkSteps } from "@/components/work-steps";
import { WorkTaskManagerModel } from "@/components/work-task-manager-model";
import { WorkCost } from "@/components/work-cost";
import { WorkControls, type WorkControlResult } from "@/components/work-controls";
import { WorkDraft } from "@/components/work-draft";
import { openWorkGate } from "@/components/work-activity-feed";
import { fetchTaskDetail, isActiveTask, TaskDetailError, type TaskDetail, type UserGate } from "@/lib/api";
import { startPolling } from "@/lib/polling";
import { callV1, V1Error } from "@/lib/v1";
import { errorText } from "@/lib/use-action";
import { TaskStatusMark } from "@/pages/tasks";
import { isMissingOperation, isUnstartedTask, markSeen, taskConversationsGet, taskTitle, workStatusLabel, workSelection, workStorage } from "@/lib/work";
import { isGateShown } from "@/lib/gates";
import { WorkConversationStateProvider, useWorkConversationStore, workConversationKey } from "@/lib/use-work-conversation-state";

const LINK = "text-sm text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** Next generation for one task's shared detail. A late result from an older number is dropped. */
function takeDetailEpoch(epochs: Map<string, number>, id: string): number {
  const next = (epochs.get(id) ?? 0) + 1;
  epochs.set(id, next);
  return next;
}

function detailEpochOf(epochs: Map<string, number>, id: string): number {
  return epochs.get(id) ?? 0;
}

/** Short screens scroll the conversation and composer together; details use the dismissible panel. */
const SHORT_COLUMN = "(max-height: 32rem) and (max-width: 1023px)";

function useMedia(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** Centre shows Manager, or the selected task. Task conversations on uses the task thread; off keeps the Planner. */
export function WorkPage({
  sub, onNavigate,
}: { sub: string; onNavigate: (sub: string, replace?: boolean) => void }) {
  return <WorkConversationStateProvider><WorkPageContent sub={sub} onNavigate={onNavigate} /></WorkConversationStateProvider>;
}

function WorkPageContent({
  sub,
  onNavigate,
}: {
  sub: string;
  onNavigate: (sub: string, replace?: boolean) => void;
}) {
  const conversations = useWorkConversationStore();
  const [detail, setDetail] = useState<{ id: string; task?: TaskDetail; error?: string; missing?: boolean }>({ id: "" });
  const [retry, setRetry] = useState(0);
  const [pane, setPane] = useState<"list" | "detail">(sub === "" ? "list" : "detail");
  const [switcher, setSwitcher] = useState(false);
  const [finder, setFinder] = useState(true);
  const [panel, setPanel] = useState(false);
  const [wide, setWide] = useState(false);
  const [draftControlId, setDraftControlId] = useState("");
  const [draftSaving, setDraftSaving] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const onControlResult = useCallback((result: WorkControlResult) => {
    setControlBusy(["requesting", "accepted", "error"].includes(result.phase));
  }, []);
  const [gateTarget, setGateTarget] = useState<{ taskId: string; step: string } | null>(null);
  const [gateError, setGateError] = useState("");
  const conversation = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const panelOpener = useRef<HTMLButtonElement>(null);
  const restorePanelFocus = useRef(false);
  const opener = useRef<HTMLElement | null>(null);
  const seen = useRef("");
  const restored = useRef(false);
  const onFinder = useCallback((available: boolean) => setFinder(available), []);
  const shortColumn = useMedia(SHORT_COLUMN);

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
  const [conversationMode, setConversationMode] = useState<Record<string, "task" | "planner">>({});
  useEffect(() => {
    if (!taskId) return;
    const ctrl = new AbortController();
    let alive = true;
    taskConversationsGet(ctrl.signal).then((result) => {
      if (alive) setConversationMode((current) => ({ ...current, [taskId]: result.enabled === true ? "task" : "planner" }));
    }).catch(() => {
      if (alive) setConversationMode((current) => ({ ...current, [taskId]: "planner" }));
    });
    return () => { alive = false; ctrl.abort(); };
  }, [taskId]);
  const cacheKey = workConversationKey({ kind: "planner", taskId });
  const candidate = detail.id === taskId && detail.task ? detail.task : conversations.cached(cacheKey)?.task;
  const selected = candidate?.task_id === taskId ? candidate : undefined;
  // Keep the same control owner through Start readback so its confirmed result survives the transition.
  const draftControls = isUnstartedTask(selected?.status) || draftControlId === taskId;
  useEffect(() => {
    if (isUnstartedTask(selected?.status)) setDraftControlId(taskId);
  }, [taskId, selected?.status]);
  useEffect(() => { setControlBusy(false); setDraftSaving(false); }, [taskId]);
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1280px)");
    const update = () => { setWide(media.matches); setPanel(false); };
    update(); media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => { setPanel(false); setGateError(""); setGateTarget(null); }, [taskId]);
  useEffect(() => {
    if (panel && !wide) panelRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    else if (restorePanelFocus.current) { restorePanelFocus.current = false; panelOpener.current?.focus(); }
  }, [panel, wide]);
  // Dismiss the mobile panel before scrolling/focusing the one conversation owner.
  useEffect(() => {
    if (!gateTarget || panel) return;
    if (gateTarget.taskId === taskId && selected?.task_id === taskId) {
      const gate = selected.gates?.find(gate => gate.step === gateTarget.step);
      const located = gate && isGateShown(gate) && conversation.current && openWorkGate(gateTarget.step, conversation.current);
      setGateError(located ? "" : gate && !isGateShown(gate)
        ? "此状态没有对话卡片，无法定位" : "关卡已更新，请刷新");
      if (!located && !wide) panelOpener.current?.focus();
    }
    setGateTarget(null);
  }, [gateTarget, panel, selected, taskId, wide]);
  const openGate = useCallback((step: string) => {
    setPanel(false); setGateTarget({ taskId, step });
  }, [taskId]);
  const closePanel = () => { restorePanelFocus.current = true; setPanel(false); };
  // One generation per task, shared by the initial read, the requirement readback, the poll and a gate update.
  const detailEpoch = useRef(new Map<string, number>());
  const selectionRef = useRef(taskId);
  selectionRef.current = taskId;
  const pageLive = useRef(true);
  useEffect(() => {
    pageLive.current = true;
    return () => { pageLive.current = false; };
  }, []);
  // Gate replies already return the new gate. Update the one shared detail without remounting its editor.
  const onGateChanged = useCallback((gate: UserGate) => {
    if (!pageLive.current || !taskId || selectionRef.current !== taskId) return;
    const current = conversations.cached(cacheKey)?.task;
    if (!current || current.task_id !== taskId) return;
    takeDetailEpoch(detailEpoch.current, taskId);
    const task = { ...current, gates: current.gates?.map(item => item.step_id === gate.step_id ? gate : item) };
    conversations.cache(cacheKey, { task });
    setDetail(current => current.id === taskId ? { ...current, task } : current);
  }, [taskId, conversations, cacheKey]);
  // Requirement edits read this task back through the shared detail. A newer read, another task, or a page that has gone, is dropped.
  const refreshDetail = useCallback(async (signal?: AbortSignal) => {
    const id = taskId;
    if (!pageLive.current || !id || selectionRef.current !== id) return;
    const token = takeDetailEpoch(detailEpoch.current, id);
    const task = await (signal ? fetchTaskDetail(id, signal) : fetchTaskDetail(id));
    if (signal?.aborted || !pageLive.current || selectionRef.current !== id || detailEpochOf(detailEpoch.current, id) !== token) return;
    if (task.task_id !== id) throw new Error("任务详情与当前任务不匹配，请刷新");
    conversations.cache(workConversationKey({ kind: "planner", taskId: id }), { task });
    setDetail(current => (
      current.id === id && detailEpochOf(detailEpoch.current, id) === token ? { id, task } : current
    ));
    return task;
  }, [taskId, conversations]);
  const active = !!selected && isActiveTask(selected);
  useEffect(() => {
    if (!taskId || !active) return;
    // One shared reader observes gates opened/closed by the Planner, not one poller per feed row.
    return startPolling(async signal => {
      const revision = takeDetailEpoch(detailEpoch.current, taskId);
      try {
        const task = await fetchTaskDetail(taskId, signal);
        if (signal.aborted || detailEpochOf(detailEpoch.current, taskId) !== revision) return;
        if (task.task_id !== taskId) throw new Error("任务详情与当前任务不匹配，请刷新");
        conversations.cache(cacheKey, { task });
        setDetail(current => (
          current.id === taskId && detailEpochOf(detailEpoch.current, taskId) === revision ? { id: taskId, task } : current
        ));
        return !isActiveTask(task);
      } catch (err) {
        if (!signal.aborted && detailEpochOf(detailEpoch.current, taskId) === revision)
          setDetail(current => current.id === taskId ? { ...current, error: errorText(err) } : current);
      }
    }, 2000);
  }, [taskId, active]);
  useEffect(() => {
    if (!taskId) return;
    const ctrl = new AbortController();
    // This read takes the task's next generation. A newer poll, gate update or requirement readback drops it.
    const token = takeDetailEpoch(detailEpoch.current, taskId);
    setDetail({ id: taskId, task: conversations.cached(cacheKey)?.task });
    // Read the latest event, not the first forward page (which can be over 1,000 events behind).
    Promise.all([
      fetchTaskDetail(taskId, ctrl.signal),
      callV1<{ activity: { seq: number }[] }>("/api/v1/tasks/activity", { task_id: taskId, limit: 1 }, { signal: ctrl.signal }),
    ]).then(([task, activity]) => {
      if (ctrl.signal.aborted) return;
      if (task.task_id !== taskId) throw new Error("任务详情与当前任务不匹配，请刷新");
      if ("status" in activity) throw new Error("Task activity is unavailable");
      const seq = Math.max(0, ...activity.activity.map(event => event.seq));
      const key = `${taskId}:${seq}`;
      if (seen.current !== key) {
        seen.current = key;
        markSeen(taskId, seq).catch((err: unknown) => console.error(err));
      }
      if (detailEpochOf(detailEpoch.current, taskId) !== token) return;
      setDetail({ id: taskId, task });
      conversations.cache(cacheKey, { task });
    }).catch((err: unknown) => {
      if (ctrl.signal.aborted || detailEpochOf(detailEpoch.current, taskId) !== token) return;
      setDetail(current => ({ id: taskId, task: current.id === taskId ? current.task : undefined,
        missing: (err instanceof TaskDetailError || err instanceof V1Error) && err.status === 404 && !isMissingOperation(err), error: errorText(err) }));
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

  const detailVisible = useMedia("(min-width: 768px)");
  const showAttention = selection.kind === "manager" && (pane === "detail" || detailVisible);

  return (
    <div className="flex h-full min-h-0 w-full">
      <div inert={panel && !wide} className={`h-full min-h-0 w-full flex-col border-border md:flex md:w-80 md:shrink-0 md:border-r ${pane === "list" ? "flex" : "hidden"}`}>
        <WorkList
          selection={selection}
          preferredProjectId={selected?.project_id ?? null}
          onOpenTask={openTask}
          onOpenManager={openManager}
          onFinder={onFinder}
          onFind={openSwitcher}
        />
      </div>
      <div className={`h-full min-h-0 min-w-0 flex-1 flex-col xl:flex-row ${pane === "detail" ? "flex" : "hidden md:flex"} ${shortColumn ? "overflow-y-auto" : ""}`} data-work-scroll={shortColumn ? "" : undefined}>
        <div inert={panel && !wide} className={shortColumn && selection.kind === "task" ? "flex h-auto min-h-full shrink-0 flex-col" : "flex h-full min-h-0 min-w-0 flex-1 flex-col"}>
          <div className="shrink-0 border-b border-border p-3 md:hidden">
            <Button type="button" variant="outline" onClick={() => setPane("list")}>返回列表</Button>
          </div>
          {selection.kind === "manager" ? (
            <>
              <header aria-label="当前任务" className="shrink-0 border-b border-border p-4">
                <h1 className="text-lg font-semibold">管家</h1>
              </header>
              <ManagerWorkConversation />
            </>
          ) : !selected ? (
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              <header aria-label="当前任务" className="space-y-3">
                {detail.id !== taskId || !detail.error ? <p role="status">正在加载任务…</p>
                  : detail.missing ? <PlannerWorkConversation taskId={taskId} missing />
                    : <div role="alert"><p>{detail.error}</p><Button variant="outline" onClick={() => setRetry(n => n + 1)}>重试</Button></div>}
              </header>
            </div>
          ) : (
            <>
              <header aria-label="当前任务" className="shrink-0 space-y-3 border-b border-border p-4">
                <h1 className="text-lg font-semibold">{taskTitle(selected.title, selected.created_at, selected.task_id)}</h1>
                <TaskStatusMark status={selected.status} label={workStatusLabel(selected.status)} />
                {selected.project_name && <p className="text-sm text-muted-foreground">{selected.project_name}</p>}
                <a className={LINK} href={`#tasks/${encodeURIComponent(taskId)}`}>打开任务页面</a>
                {!wide && <Button ref={panelOpener} variant="outline" aria-haspopup="dialog" aria-expanded={panel}
                  onClick={() => setPanel(true)}>任务详情</Button>}
                {gateError && <p role="alert" className="text-sm text-destructive">{gateError}</p>}
                {detail.error && <p role="alert" className="text-sm text-destructive">任务刷新失败： {detail.error}</p>}
              </header>
              {draftControls && <WorkControls key={taskId} task={selected} onRefresh={refreshDetail}
                disabled={draftSaving} compact onResult={onControlResult} />}
              <div ref={conversation} data-work-detail="" data-layout={shortColumn ? "scroll" : "split"}
                className={shortColumn ? "work-short-conversation block" : "flex min-h-0 min-w-0 flex-1 flex-col"}>
                {isUnstartedTask(selected.status) ? <WorkDraft key={taskId} task={selected} onRefresh={refreshDetail}
                  disabled={controlBusy} onBusy={setDraftSaving} />
                  : !conversationMode[taskId] ? <p role="status">正在检查对话可用性</p>
                  : conversationMode[taskId] === "task"
                    ? <TaskWorkConversation key={taskId} taskId={taskId} detail={selected} task={selected} onGateChanged={onGateChanged} />
                    : <PlannerWorkConversation key={taskId} taskId={taskId} detail={selected} task={selected} onGateChanged={onGateChanged} />}
              </div>
            </>
          )}
        </div>
        {showAttention && <WorkAttention />}
      </div>
      {selected && (wide || panel) && <aside ref={panelRef} aria-label="任务详情"
        role={wide ? undefined : "dialog"} aria-modal={wide ? undefined : true}
        onKeyDown={event => {
          if (wide) return;
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closePanel(); }
          if (event.key === "Tab") {
            const buttons = [...(panelRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), a[href], summary") ?? [])];
            const first = buttons[0], last = buttons[buttons.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
          }
        }}
        className={wide ? "min-h-0 w-72 shrink-0 overflow-y-auto border-l border-border"
          : "fixed inset-0 z-50 overflow-y-auto bg-background"}>
        {!wide && <div className="border-b border-border p-3"><Button variant="outline" onClick={closePanel}>关闭任务详情</Button></div>}
        {!draftControls && <WorkControls task={selected} onRefresh={refreshDetail} />}
        <WorkRequirement task={selected} onRefresh={async () => { await refreshDetail(); }} />
        <WorkTaskManagerModel
          mode="task"
          taskId={taskId}
          snapshot={null}
          onReread={() => Promise.reject(new Error("task manager profile read is not available"))}
        />
        <WorkSteps task={selected} onOpenGate={step => openGate(step.step_key)} />
        <WorkGates task={selected} onOpenGate={openGate} />
        <div className="p-4"><WorkChanges taskId={taskId} visible pendingWorkspace={selected.status === "draft"} /></div>
        <WorkCost task={selected} />
      </aside>}
      <WorkSwitcher open={switcher && finder} onClose={closeSwitcher} onOpen={openTask} restore={restore} />
    </div>
  );
}
