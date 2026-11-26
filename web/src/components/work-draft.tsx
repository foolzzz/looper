import { useEffect, useRef, useState } from "react";
import { WORK_COMPOSER_COPY } from "@/components/work-conversation";
import { ChatComposer } from "@/components/chat-composer";
import { WorkTaskManagerModel, type TaskManagerSnapshot } from "@/components/work-task-manager-model";
import { Button } from "@/components/ui/button";
import { editTaskRequirement, type TaskDetail } from "@/lib/api";
import { getTaskManagerProfile, type TaskManagerProfileView } from "@/lib/task-manager-profile";
import { useWorkConversationState } from "@/lib/use-work-conversation-state";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";
import { isUnstartedTask } from "@/lib/work";

function snapshotFrom(view: TaskManagerProfileView): TaskManagerSnapshot {
  const effectiveName = view.choices.find(choice => choice.profile_id === view.effective.profile_id)?.label
    ?? view.effective.profile_id;
  return {
    choices: view.choices.map(choice => ({
      profile_id: choice.profile_id,
      name: choice.label,
      model: choice.model,
      runtime: choice.runtime,
      provider: "",
      source: "",
    })),
    effective: {
      profile_id: view.effective.profile_id,
      name: effectiveName,
      model: view.effective.model,
      runtime: view.effective.runtime,
      provider: view.effective.provider,
      source: view.source,
    },
    inherit: true,
    selectedProfileId: view.override_profile_id,
  };
}

/** Reads the draft's Manager profile. Saving stays inside the picker and does not start the task. */
function DraftTaskManager({ taskId }: { taskId: string }) {
  const [snapshot, setSnapshot] = useState<TaskManagerSnapshot | null>(null);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setError("");
    getTaskManagerProfile(taskId, controller.signal).then(view => {
      if (live) setSnapshot(view ? snapshotFrom(view) : null);
    }).catch((err: unknown) => {
      if (!live || controller.signal.aborted) return;
      setSnapshot(null);
      setError(errorText(err));
    });
    return () => { live = false; controller.abort(); };
  }, [taskId, nonce]);
  const reread = async () => {
    const view = await getTaskManagerProfile(taskId);
    if (!view) throw new Error("任务管家模型读取失败");
    const next = snapshotFrom(view);
    setSnapshot(next);
    return next;
  };
  if (error && !snapshot) {
    return <section aria-label="任务管家模型" className="space-y-2 border-b border-border p-4">
      <h2 className="text-sm font-semibold">任务管家模型</h2>
      <p role="alert" className="break-words text-sm text-destructive">{error}</p>
      <Button type="button" variant="outline" onClick={() => setNonce(value => value + 1)}>重新读取</Button>
    </section>;
  }
  return <WorkTaskManagerModel mode="task" taskId={taskId} snapshot={snapshot} onReread={reread} />;
}

/** Unstarted task messages edit the requirement; only the separate Start control launches a Planner. */
export function WorkDraft({ task, onRefresh, disabled, onBusy }: {
  task: TaskDetail;
  onRefresh: () => Promise<TaskDetail | undefined>;
  disabled: boolean;
  onBusy: (busy: boolean) => void;
}) {
  const composer = useWorkConversationState({ kind: "planner", taskId: task.task_id });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const pending = useRef<{ requirement: string; text: string } | null>(null);
  const locked = useRef(false);
  const live = useRef(true);
  const footer = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef(false);
  useEffect(() => {
    live.current = true;
    return () => { live.current = false; onBusy(false); };
  }, [onBusy]);
  useEffect(() => {
    if (busy || !restoreFocus.current) return;
    restoreFocus.current = false;
    // Disabling the input blurs it in browsers. Do not steal focus from another control.
    if (document.activeElement === document.body || footer.current?.contains(document.activeElement))
      footer.current?.querySelector("textarea")?.focus();
  }, [busy]);

  const lock = (value: boolean) => { locked.current = value; setBusy(value); onBusy(value); };
  const readback = async () => {
    const current = await onRefresh();
    if (!live.current) return;
    if (!current || current.task_id !== task.task_id) throw new Error("尚未读到已保存的需求，请重新读取");
    if (current.requirement !== pending.current?.requirement)
      throw new Error("需求已在其他窗口修改，已显示最新版本；请核对后重新发送");
    // Clear only after server readback, never an optimistic message bubble.
    if (composer.value.draft === pending.current.text) composer.setDraft("");
    composer.rememberHistory([...composer.value.history, pending.current.text]);
    pending.current = null;
    setUncertain(false); setError(""); lock(false);
  };
  const send = async (text: string) => {
    if (disabled || locked.current || !text.trim()) return;
    restoreFocus.current = !!footer.current?.contains(document.activeElement);
    lock(true); setError("");
    let writing = false;
    try {
      // Append to a fresh server value, including edits made in the right panel.
      const current = await onRefresh();
      if (!live.current) return;
      if (!current || current.task_id !== task.task_id || !isUnstartedTask(current.status))
        throw new Error("任务已不在待开始阶段，请重新读取任务");
      const requirement = current.requirement ? `${current.requirement}\n\n${text}` : text;
      pending.current = { requirement, text };
      writing = true;
      const version = Math.max(1, ...(current.requirement_versions ?? []).map(item => item.version));
      await editTaskRequirement(task.task_id, requirement, version);
      if (live.current) await readback();
    } catch (err) {
      if (!live.current) return;
      if (err instanceof V1Error && err.code === "requirement_changed") {
        pending.current = null; setUncertain(false);
        try {
          const latest = await onRefresh();
          if (!latest || latest.task_id !== task.task_id) throw new Error("未能读取最新需求");
          if (live.current) setError("需求已在其他窗口修改，已显示最新版本；请核对后重新发送");
        } catch (refreshError) {
          if (live.current) setError(`需求已在其他窗口修改，读取最新需求失败：${errorText(refreshError)}`);
        }
        return;
      }
      setError(errorText(err));
      // A failed read/network write may already have saved. Read retry never repeats the write.
      const unknown = writing && !(err instanceof V1Error && err.status >= 400 && err.status < 500);
      setUncertain(unknown);
      if (!unknown) pending.current = null;
    } finally {
      if (live.current) lock(false);
    }
  };
  const retry = async () => {
    if (locked.current) return;
    lock(true);
    try { await readback(); }
    catch (err) { if (live.current) setError(errorText(err)); }
    finally { if (live.current) lock(false); }
  };
  const versions = task.requirement_versions ?? [];
  const version = versions.length ? Math.max(...versions.map(item => item.version)) : task.requirement ? 1 : null;
  return <section aria-label="草稿编辑" className="flex min-h-0 flex-1 flex-col">
    <div className="min-h-0 flex-1 overflow-y-auto">
      <DraftTaskManager taskId={task.task_id} />
      <section aria-label="需求草稿" className="space-y-3 p-4">
        <h2 className="text-sm font-semibold">需求草稿{version !== null && <span className="ml-2 text-xs font-normal text-muted-foreground">版本 {version}</span>}</h2>
        {task.requirement ? <p className="whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]">{task.requirement}</p>
          : <p className="text-sm text-muted-foreground">描述你想完成的任务。发送的内容会保存为需求草稿。</p>}
      </section>
    </div>
    <div ref={footer} className="shrink-0 space-y-2 border-t border-border p-4">
      <p className="text-xs text-muted-foreground">继续发送会追加到需求。点击「开始」后，Planner 才会启动。</p>
      {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
      {uncertain && <Button variant="outline" onClick={() => void retry()}>重新读取需求</Button>}
      {busy && !uncertain && <p role="status" className="text-xs text-muted-foreground">正在保存需求…</p>}
      <ChatComposer draft={composer.value.draft} onDraft={composer.setDraft}
        staged={composer.value.staged} onStaged={composer.setStaged} history={composer.value.history}
        replying={false} canStop={false} onStop={() => {}} controls="neutral" copy={WORK_COMPOSER_COPY}
        disabled={disabled || busy} onSend={text => void send(text)} />
    </div>
  </section>;
}
