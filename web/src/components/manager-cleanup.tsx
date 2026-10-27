import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { previewManagerCleanup, purgeManagerCleanup, type ManagerCleanupResult } from "@/lib/manager-cleanup-api";
import { errorText } from "@/lib/use-action";

const IMPACT = "聊天消息正文、模型运行记录及其事件、Manager 操作历史将永久删除，无法恢复。Manager 身份和会话归属记录永久保留，旧身份与旧会话不能重新分配；不会删除运行文件。";

/** Operator-only cleanup is deliberately separate from the live Manager list. */
export function ManagerCleanup() {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [managerId, setManagerId] = useState("");
  const [retention, setRetention] = useState("30");
  const [result, setResult] = useState<ManagerCleanupResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"preview" | "purge" | null>(null);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const request = useRef<AbortController | null>(null);
  const version = useRef(0);
  const submitting = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const purgeButton = useRef<HTMLButtonElement>(null);
  const days = Number(retention);
  const valid = !!managerId.trim() && /^\d+$/.test(retention) && Number.isInteger(days) && days >= 30 && days <= 3650;
  const current = result?.manager_id === managerId.trim() && result.retention_days === days;
  const canPurge = current && result.eligible && !result.purged && !refreshRequired;

  useEffect(() => () => { version.current++; request.current?.abort(); }, []);
  useEffect(() => {
    if (confirming) dialog.current?.showModal();
    else if (dialog.current?.open) {
      dialog.current.close();
      purgeButton.current?.focus();
    }
  }, [confirming]);

  const invalidate = () => {
    version.current++;
    request.current?.abort();
    submitting.current = false;
    setBusy(null);
    setResult(null);
    setError("");
    setRefreshRequired(false);
    setConfirming(false);
  };

  const run = async (operation: "preview" | "purge") => {
    if (submitting.current || !valid || (operation === "purge" && !canPurge)) return;
    submitting.current = true;
    const sequence = ++version.current;
    const controller = new AbortController();
    request.current = controller;
    const target = { manager_id: managerId.trim(), retention_days: days };
    setBusy(operation);
    setError("");
    setConfirming(false);
    try {
      const answer = await (operation === "preview" ? previewManagerCleanup : purgeManagerCleanup)(target, controller.signal);
      if (sequence !== version.current) return;
      if (answer.manager_id !== target.manager_id || answer.retention_days !== target.retention_days) {
        throw new Error("返回结果与当前目标不一致，请重新预览。");
      }
      setResult(answer);
      setRefreshRequired(false);
    } catch (err) {
      if (sequence !== version.current) return;
      setError(errorText(err));
      setRefreshRequired(true);
    } finally {
      if (sequence === version.current) { submitting.current = false; setBusy(null); }
    }
  };

  return (
    <div>
      <Button type="button" variant="outline" className="min-h-11 max-w-full whitespace-normal text-left" aria-expanded={open}
        aria-controls={`${id}-panel`} disabled={busy === "purge"}
        onClick={() => { setOpen(!open); if (open) invalidate(); }}>
        清理已删除 Manager 的历史（仅操作员）
      </Button>
      {open && <Card id={`${id}-panel`} className="mt-3">
        <CardHeader><CardTitle>永久清理历史内容</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">输入已知的已删除 Manager ID。仅支持删除至少 30 天且已完全停止的 Manager；服务器在执行时重新检查。</p>
          <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void run("preview"); }}>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <label htmlFor={`${id}-manager`} className="text-sm">已删除 Manager ID</label>
                <Input id={`${id}-manager`} className="min-h-11" value={managerId} required disabled={busy === "purge"}
                  onChange={(event) => { invalidate(); setManagerId(event.target.value); }} />
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-retention`} className="text-sm">删除后保留天数</label>
                <Input id={`${id}-retention`} className="min-h-11" type="number" min={30} max={3650} step={1}
                  value={retention} required disabled={busy === "purge"}
                  onChange={(event) => { invalidate(); setRetention(event.target.value); }} />
                <p className="text-xs text-muted-foreground">默认 30 天，可选 30–3650 天。</p>
              </div>
            </div>
            <Button type="submit" variant="outline" className="min-h-11" disabled={!valid || !!busy}>
              {busy === "preview" ? "正在预览…" : "预览清理"}
            </Button>
          </form>
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
          {current && result && <div className="space-y-3">
            <p role="status" className="break-words text-sm">{busy === "purge" ? "正在永久清理，请等待服务器结果" : result.purged ? "已完成永久清理" : result.eligible ? "符合清理条件" : "暂不符合清理条件"}：{result.manager_id}；保留期 {result.retention_days} 天。</p>
            {result.deleted_at && <p className="break-words text-xs text-muted-foreground">删除时间：{result.deleted_at}</p>}
            {result.blockers.length > 0 && <ul className="list-inside list-disc break-words text-sm text-warning">
              {result.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
            </ul>}
            <p className="text-sm">保留会话归属记录：{result.counts.threads} 条（不删除）</p>
            <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2" aria-label={result.purged ? "实际删除数量" : "待删除数量"}>
              {([["聊天消息", result.counts.chat_messages], ["模型运行记录", result.counts.llm_records],
                ["运行事件", result.counts.llm_record_events], ["操作历史", result.counts.manager_actions]] as const)
                .map(([label, count]) => <div key={label} className="flex justify-between gap-3"><dt>{result.purged ? "已删除" : "待删除"}{label}</dt><dd>{count}</dd></div>)}
            </dl>
            <p className="text-sm text-muted-foreground">{IMPACT}</p>
            {refreshRequired && <p className="text-sm text-warning">上次请求失败，请重新预览后再清理。</p>}
            {!result.purged && <Button ref={purgeButton} type="button" variant="destructive" className="min-h-11"
              disabled={!canPurge || !!busy} onClick={() => setConfirming(true)}>永久清理…</Button>}
          </div>}
          <dialog ref={dialog} aria-labelledby={`${id}-confirm-title`} aria-describedby={`${id}-impact`}
            onCancel={() => setConfirming(false)}
            onKeyDown={(event) => {
              if (event.key !== "Tab") return;
              const buttons = event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
              const first = buttons[0], last = buttons[buttons.length - 1];
              if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
              else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
            }}
            className="m-auto max-h-full w-full max-w-md space-y-4 overflow-y-auto rounded-lg border border-border bg-card p-5 text-card-foreground backdrop:bg-background/80">
            <h2 id={`${id}-confirm-title`} className="break-words text-lg font-semibold">确认永久清理 {result?.manager_id}？</h2>
            <p id={`${id}-impact`} className="text-sm">{IMPACT}</p>
            <p className="text-sm">保留期 {result?.retention_days} 天。执行时会再次检查当前条件和数量。</p>
            <div className="flex flex-wrap justify-end gap-2">
              <Button type="button" variant="outline" className="min-h-11" autoFocus onClick={() => setConfirming(false)}>取消</Button>
              <Button type="button" variant="destructive" className="min-h-11" disabled={!!busy || !canPurge}
                onClick={() => void run("purge")}>确认永久清理</Button>
            </div>
          </dialog>
        </CardContent>
      </Card>}
    </div>
  );
}
