import { useEffect, useRef, useState } from "react";
import { ChatComposer } from "@/components/chat-composer";
import { Button } from "@/components/ui/button";
import { editTaskRequirement, type TaskDetail } from "@/lib/api";
import { useWorkConversationState } from "@/lib/use-work-conversation-state";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";

/** Draft messages edit the persisted requirement; only the separate Start control launches a Planner. */
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
      throw new Error("尚未确认本次需求保存，请重新读取；可在右侧查看服务端需求");
    // Clear only after server readback, never an optimistic message bubble.
    composer.setDraft("");
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
      if (!current || current.task_id !== task.task_id || current.status !== "draft")
        throw new Error("任务已不在草稿阶段，请重新读取任务");
      const requirement = current.requirement ? `${current.requirement}\n\n${text}` : text;
      pending.current = { requirement, text };
      writing = true;
      await editTaskRequirement(task.task_id, requirement);
      if (live.current) await readback();
    } catch (err) {
      if (!live.current) return;
      setError(errorText(err));
      // A failed read/network write may already have saved. Only retry its read, never its write.
      const unknown = writing && !(err instanceof V1Error && err.status >= 400 && err.status < 500);
      setUncertain(unknown);
      if (!unknown) { pending.current = null; lock(false); }
    }
  };
  const retry = async () => {
    try { await readback(); }
    catch (err) { if (live.current) setError(errorText(err)); }
  };
  const versions = task.requirement_versions ?? [];
  const version = versions.length ? Math.max(...versions.map(item => item.version)) : null;
  return <section aria-label="草稿编辑" className="flex min-h-0 flex-1 flex-col">
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <section aria-label="需求草稿" className="space-y-3">
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
        replying={false} canStop={false} onStop={() => {}} controls="neutral"
        disabled={disabled || busy} onSend={text => void send(text)} />
    </div>
  </section>;
}
