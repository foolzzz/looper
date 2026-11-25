import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { startReadyTask, startTask, stopTask, type TaskDetail } from "@/lib/api";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";
import { callV1, V1Error } from "@/lib/v1";
import { isMissingOperation } from "@/lib/work";

const ACTIVE = new Set(["assigned", "running", "planning", "executing", "verifying", "stopping", "waiting_user", "awaiting_budget", "waiting", "pending"]);
const TERMINAL = new Set(["completed", "failed", "stopped", "cancelled"]);
const RESTARTABLE = new Set(["failed", "stopped", "cancelled"]);
type Action = "start" | "stop" | "done";
export type WorkControlResult = { phase: "idle" | "requesting" | "accepted" | "settled" | "error"; message?: string; warning?: string };
type Request = { action: Action; before: TaskDetail; controller: AbortController; terminal?: string; reads: number; warning?: string; rejected?: boolean };
type Props = {
  task: TaskDetail;
  disabled?: boolean;
  compact?: boolean;
  onRefresh: (signal?: AbortSignal) => Promise<TaskDetail | undefined>;
  onResult?: (result: WorkControlResult) => void;
};

/** Only mounted within the Work gate. Task identity owns the request and aborts all readback on a switch. */
export function WorkControls(props: Props) {
  return <Controls key={props.task.task_id} {...props} />;
}

function Controls({ task, onRefresh, onResult, disabled, compact }: Props) {
  const [result, setResult] = useState<WorkControlResult>({ phase: "idle" });
  const [unavailable, setUnavailable] = useState<Action | null>(null);
  const request = useRef<Request | null>(null);
  const reading = useRef(false);
  const live = useRef(true);
  const refresh = useRef(onRefresh);
  refresh.current = onRefresh;
  useEffect(() => {
    live.current = true;
    return () => { live.current = false; request.current?.controller.abort(); request.current = null; };
  }, []);
  useEffect(() => { onResult?.(result); }, [result, onResult]);
  const owns = (pending: Request) => live.current && request.current === pending && !pending.controller.signal.aborted;

  const read = async (pending: Request): Promise<boolean> => {
    if (!owns(pending) || reading.current) return false;
    reading.current = true;
    try {
      const current = await refresh.current(pending.controller.signal);
      if (!owns(pending)) return true;
      // Another shared read can supersede this one. It is never evidence for this operation.
      if (!current) return false;
      if (current.task_id !== task.task_id) throw new Error("任务详情与当前任务不匹配，请刷新");
      if (pending.rejected) {
        request.current = null;
        setResult({ phase: "idle" });
        return true;
      }
      let confirmed = pending.action === "done" ? current.status === "completed"
        : pending.action === "stop" ? TERMINAL.has(current.status) : ACTIVE.has(current.status) && current.status !== "stopping";
      if (pending.action === "start" && TERMINAL.has(current.status)) {
        // Same algorithm as the old detail: aggregate run counts alone do not identify a new attempt.
        const endChanged = current.attempt_ended_at != null && current.attempt_ended_at !== pending.before.attempt_ended_at;
        const changed = current.status !== pending.before.status || endChanged;
        const settled = endChanged || !current.cost?.running;
        const snapshot = JSON.stringify([current.status, current.attempt_ended_at, current.cost?.runs]);
        confirmed = changed && settled && pending.terminal === snapshot;
        pending.terminal = changed && settled ? snapshot : undefined;
      } else pending.terminal = undefined;
      if (confirmed) {
        request.current = null;
        setResult({ phase: "settled", warning: pending.warning,
          message: current.status === "failed" ? current.error || current.result_summary || "后台任务失败" : undefined });
        return true;
      }
      if (++pending.reads >= 15) {
        setResult({ phase: "error", message: "尚未读到本次操作的真实状态", warning: pending.warning });
        return true;
      }
      return false;
    } catch (err) {
      if (owns(pending)) {
        pending.terminal = undefined;
        setResult({ phase: "error", message: errorText(err), warning: pending.warning });
      }
      return true;
    } finally { reading.current = false; }
  };

  useEffect(() => {
    const pending = request.current;
    if (result.phase !== "accepted" || !pending) return;
    // The page owns detail/cache updates; this bounded observer only requests that shared refresh.
    return startPolling(() => read(pending), 2000);
  }, [result.phase]);

  const run = async (action: Action) => {
    if (disabled || request.current) return; // Synchronous lock, before React disables the controls.
    const pending: Request = { action, before: task, controller: new AbortController(), reads: 0, rejected: true };
    request.current = pending;
    setResult({ phase: "requesting" });
    try {
      // Freeze a fresh baseline before the write: a late Stop must not look like the new attempt.
      const before = await refresh.current(pending.controller.signal);
      if (!owns(pending)) return;
      if (!before || before.task_id !== task.task_id) throw new Error("未能读取操作前的任务状态");
      pending.before = before;
      if (action === "start") {
        if (before.status === "draft" && (!before.requirement?.trim() || !before.work_dir?.trim()))
          throw new Error("草稿需要非空需求和有效目录");
        if (before.status !== "init" && before.status !== "draft" && !RESTARTABLE.has(before.status))
          throw new Error("任务状态已变化，请重新读取");
        pending.rejected = false;
        if (RESTARTABLE.has(before.status)) await startTask(task.task_id, { startOver: false });
        else await startReadyTask(task.task_id);
      } else if (action === "stop") {
        if (!ACTIVE.has(before.status)) throw new Error("任务状态已变化，请重新读取");
        pending.rejected = false;
        await stopTask(task.task_id);
      } else {
        if (ACTIVE.has(before.status)) throw new Error("请先停止后台任务或等待结束");
        if (before.status !== "init" && before.status !== "draft" && before.status !== "completed" && !RESTARTABLE.has(before.status))
          throw new Error("未知任务状态，控制暂不可用");
        pending.rejected = false;
        const done = await callV1<{ task_id: string; status: string; changed: boolean; warning: string | null }>(
          "/api/v1/tasks/done", { task_id: task.task_id });
        if (done.status === "needs_confirmation") throw new V1Error(202, "unavailable", "impact" in done ? done.impact : "需要确认");
        if (!("task_id" in done) || done.task_id !== task.task_id || done.status !== "completed") throw new Error("完成操作未返回有效结果");
        pending.warning = done.warning ?? undefined;
      }
      if (owns(pending)) setResult({ phase: "accepted", warning: pending.warning });
    } catch (err) {
      if (!owns(pending)) return;
      // A 4xx rejection is safe to unlock after a fresh read. Network/202 remain uncertain: never repeat the write.
      pending.rejected ||= err instanceof V1Error && err.status >= 400 && err.status < 500;
      if (isMissingOperation(err)) setUnavailable(action);
      setResult({ phase: "error", message: err instanceof V1Error && err.status === 409
        ? `${errorText(err)}。请先停止这一轮，再重新读取任务状态。` : errorText(err) });
    }
  };
  const retryRead = () => {
    const pending = request.current;
    if (!pending || reading.current) return;
    pending.reads = 0;
    void read(pending);
  };
  const startable = task.status === "init" || task.status === "draft" || RESTARTABLE.has(task.status);
  const direct = startable || task.status === "completed";
  const blocked = disabled || result.phase === "requesting" || result.phase === "accepted" || result.phase === "error";
  const draftReason = task.status === "draft" ? !task.requirement?.trim() ? "请先填写需求" : !task.work_dir?.trim() ? "请先选择有效目录" : "" : "";
  const startUnavailable = unavailable === "start" ? task.status === "draft" ? "此服务器尚未提供草稿开始入口" : "开始暂不可用" : "";
  return <section aria-labelledby="work-controls-title" className="min-w-0 p-4">
    <h2 id="work-controls-title" className="text-sm font-semibold">任务控制</h2>
    <div className="mt-2 flex flex-wrap gap-2">
      {startable && <Button variant="outline" disabled={blocked || !!draftReason || !!startUnavailable} aria-describedby="work-start-reason" onClick={() => void run("start")}>
        {RESTARTABLE.has(task.status) ? "继续后台任务" : "开始"}
      </Button>}
      {ACTIVE.has(task.status) && <Button variant="outline" disabled={blocked || unavailable === "stop"} onClick={() => void run("stop")}>停止后台任务</Button>}
      {direct && <Button variant="outline" disabled={blocked || unavailable === "done" || task.status === "completed"} onClick={() => void run("done")}>完成</Button>}
      {!compact && ["暂停", "交给后台", "接手"].map(label => <Button key={label} variant="outline" disabled aria-describedby="work-future-control-reason">{label}</Button>)}
    </div>
    <p id="work-start-reason" className="mt-2 text-xs text-muted-foreground">{draftReason || startUnavailable}</p>
    {unavailable === "done" && <p className="mt-2 text-xs text-muted-foreground">完成暂不可用</p>}
    {unavailable === "stop" && <p className="mt-2 text-xs text-muted-foreground">停止后台任务暂不可用</p>}
    {!direct && !ACTIVE.has(task.status) && <p className="mt-2 text-xs text-muted-foreground">未知任务状态，控制暂不可用</p>}
    {!compact && <p id="work-future-control-reason" className="mt-2 text-xs text-muted-foreground">此控制将在后续步骤提供</p>}
    {result.phase !== "idle" && <p role="status" className="mt-2 text-sm">
      {result.phase === "requesting" ? "正在请求 · 操作尚未确认" : result.phase === "accepted" ? "服务器已接受 · 操作尚未确认" : result.phase === "settled" ? "操作已确认" : "操作尚未确认"}
    </p>}
    {result.message && <p role="alert" className="mt-2 break-words text-sm text-destructive">{result.message}</p>}
    {result.warning && <p role="status" className="mt-2 break-words text-sm text-warning">{result.warning}</p>}
    {(result.phase === "accepted" || result.phase === "error") && <Button variant="outline" className="mt-2" onClick={retryRead}>重新读取</Button>}
  </section>;
}
