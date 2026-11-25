import { useEffect, useId, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchEngineSettings, isConfirmation, updateEngineSettings, type EngineSettings, type FailureMode } from "@/lib/api-task-failures";
import { V1Error, type V1PendingConfirmation } from "@/lib/v1";

const failureText = (err: unknown) => err instanceof V1Error && err.code === "unavailable" ? `服务暂时不可用：${err.message}` : String(err);

export function EngineSettingsSection() {
  const id = useId();
  const [saved, setSaved] = useState<EngineSettings | null>(null);
  const [value, setValue] = useState<FailureMode | "default">("default");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<V1PendingConfirmation | null>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const saveButton = useRef<HTMLButtonElement>(null);
  const load = async () => {
    setError("");
    try { const result = await fetchEngineSettings(); setSaved(result); setValue(result.failure_typing.source === "built-in" ? "default" : result.failure_typing.value); }
    catch (err) { setError(failureText(err)); }
  };
  useEffect(() => { void load(); }, []);
  useEffect(() => { if (pending) cancel.current?.focus(); else saveButton.current?.focus(); }, [pending]);
  const save = async (confirmation?: V1PendingConfirmation) => {
    setBusy(true); setError("");
    try {
      const result = await updateEngineSettings(value === "default" ? null : value, confirmation);
      if (isConfirmation(result)) setPending(result);
      else { setSaved(result); setPending(null); setValue(result.failure_typing.source === "built-in" ? "default" : result.failure_typing.value); }
    } catch (err) { setError(failureText(err)); setPending(null); }
    finally { setBusy(false); }
  };
  const dirty = saved && value !== (saved.failure_typing.source === "built-in" ? "default" : saved.failure_typing.value);
  return <Card><CardHeader><CardTitle>引擎失败处理</CardTitle></CardHeader><CardContent className="space-y-4">
    <p className="text-sm text-muted-foreground">默认关闭。record 仅记录失败类型；act 会有界重试额度或瞬时故障，并将环境问题和连续失败交给你处理。</p>
    {saved ? <><div className="flex flex-wrap items-center gap-2"><Badge variant="outline">当前：{saved.failure_typing.value}</Badge><span className="text-xs text-muted-foreground">来源：{saved.failure_typing.source === "built-in" ? "内置默认" : "系统设置"}</span></div>
      <div className="space-y-2"><label htmlFor={id} className="text-sm font-medium">失败处理模式</label>
        <select id={id} value={value} disabled={busy || pending !== null} onChange={event => setValue(event.target.value as FailureMode | "default")}
          className="block w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring pointer-coarse:min-h-11 pointer-coarse:text-base">
          <option value="default">使用内置默认（off）</option><option value="off">off · 关闭</option><option value="record">record · 仅记录</option><option value="act">act · 处理失败</option></select></div>
      <p className="text-xs text-muted-foreground">从 act 切到其他模式会解除已有失败等待，交回原失败路径；历史保留。账号准入和限流信号开关独立。</p>
      <Button ref={saveButton} disabled={busy || pending !== null || !dirty} onClick={() => void save()}>保存模式</Button>
    </> : !error && <p role="status" className="text-sm text-muted-foreground">正在读取设置…</p>}
    {pending && <div role="group" aria-label="确认引擎设置" className="space-y-3 rounded-md border border-border p-4"><p className="whitespace-pre-wrap break-words text-sm">{pending.impact}</p><div className="flex flex-wrap gap-2"><Button disabled={busy} onClick={() => void save(pending)}>确认保存</Button><Button ref={cancel} variant="outline" disabled={busy} onClick={() => setPending(null)}>取消</Button></div></div>}
    {error && <div className="space-y-2"><p role="alert" className="break-words text-sm text-destructive">{error}</p>{!saved && <Button variant="outline" disabled={busy} onClick={() => void load()}>重新读取</Button>}</div>}
  </CardContent></Card>;
}
