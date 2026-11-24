import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { fetchFenceSettings, fetchOutboundReviewSettings, updateFenceSettings, updateOutboundReviewSettings,
  type FenceSettings, type UnsupportedManager } from "@/lib/api-security";
import { V1Error, type V1PendingConfirmation } from "@/lib/v1";

type SettingsValue = FenceSettings & { managers_without_hook?: UnsupportedManager[] };
const description = (review: boolean, enabled: boolean) => review
  ? enabled ? "打开后，每次需要审查的对外动作增加一次无工具判定；判断不了会拦截。" : "关闭后，对外动作不再经过审查；已有审计仍可查。"
  : enabled ? "打开后，Looper 注入的外部内容会带上出处和不可信围栏；不改变审查开关。" : "关闭后，Looper 注入的外部内容恢复原有文本；不改变审查开关。";

function SecuritySwitch({ review }: { review: boolean }) {
  const title = review ? "对外动作审查" : "不可信内容围栏";
  const read = review ? fetchOutboundReviewSettings : fetchFenceSettings;
  const update = review ? updateOutboundReviewSettings : updateFenceSettings;
  const [saved, setSaved] = useState<SettingsValue | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [intent, setIntent] = useState<boolean | null>(null);
  const [pending, setPending] = useState<V1PendingConfirmation | null>(null);
  const [needsRead, setNeedsRead] = useState(false);
  const [retryIntent, setRetryIntent] = useState<boolean | null>(null);
  const [revision, setRevision] = useState(0);
  const area = useRef<HTMLDivElement>(null);
  const returnFocus = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void read(controller.signal).then((value) => {
      if (controller.signal.aborted) return;
      setSaved(value); setError(""); setForbidden(false); setNeedsRead(false);
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : String(reason));
      setForbidden(reason instanceof V1Error && reason.status === 403);
      setNeedsRead(true);
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [read, revision]);

  useEffect(() => {
    if (intent !== null) area.current?.querySelector<HTMLButtonElement>('[role="dialog"] button')?.focus();
    else if (returnFocus.current && !busy) {
      area.current?.querySelector<HTMLButtonElement>('[role="switch"]')?.focus(); returnFocus.current = false;
    }
  }, [intent, busy]);

  function cancel() { setIntent(null); setPending(null); returnFocus.current = true; }
  async function save() {
    if (intent === null) return;
    setBusy(true); setError("");
    try {
      const value = await update(intent, pending ?? undefined);
      if ("status" in value) { setPending(value); return; }
      setSaved(value); setIntent(null); setPending(null); setRetryIntent(null); returnFocus.current = true;
      // The write response is the saved value. The follow-up read refreshes compatibility only.
      const current = await read();
      setSaved({ ...value, ...(review ? { managers_without_hook: (current as SettingsValue).managers_without_hook } : {}) });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setForbidden(reason instanceof V1Error && reason.status === 403);
      setRetryIntent(intent); setIntent(null); setPending(null); setNeedsRead(true); returnFocus.current = true;
    } finally { setBusy(false); }
  }
  return <Card className="min-w-0">
    <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
    <CardContent><div ref={area} className="space-y-4">
      <p className="text-sm text-muted-foreground">{review
        ? "增加一次只读动作及触发上下文、不带工具的判定。判断不了会拦截。此前单次实测约数美元千分之几、数秒，实际费用与耗时会变化。"
        : "只给 Looper 注入的外部内容加上带出处的不可信围栏；独立于动作审查。"}</p>
      {forbidden ? <p role="alert" className="text-sm">仅操作者可查看和修改此设置。</p> : <>
        {loading && <p role="status" aria-label={`读取${title}`} className="min-h-6 text-sm text-muted-foreground">正在读取{title}…</p>}
        {saved && <div className="flex items-center justify-between gap-4">
          <Badge variant="outline">已保存：{saved.enabled ? "打开" : "关闭"}</Badge>
          <Switch label={title} checked={saved.enabled} disabled={busy || loading || needsRead || intent !== null}
            onChange={(enabled) => { setIntent(enabled); setPending(null); }} />
        </div>}
        {error && <div className="space-y-2"><p role="alert" className="break-words text-sm text-destructive">{title}不可读或未保存：{error}</p>
          <Button variant="outline" disabled={loading || busy} onClick={() => { setPending(null); setRevision((n) => n + 1); }}>重新读取</Button>
        </div>}
        {!needsRead && retryIntent !== null && intent === null && <Button variant="outline" disabled={loading || busy}
          onClick={() => setIntent(retryIntent)}>重试{retryIntent ? "打开" : "关闭"}</Button>}
        {intent !== null && <div role="dialog" aria-label={`确认${title}`} className="space-y-3 rounded-md border border-border p-3"
          onKeyDown={(event) => { if (event.key === "Escape" && !busy) cancel(); }}>
          <p className="text-sm">{pending?.impact ?? description(review, intent)}</p>
          <div className="flex flex-wrap gap-2"><Button disabled={busy} onClick={() => void save()}>{busy ? "正在保存…" : pending ? "确认执行" : "确认更改"}</Button>
            <Button variant="outline" disabled={busy} onClick={cancel}>取消</Button></div>
        </div>}
        {review && !!saved?.managers_without_hook?.length && <div className="space-y-2 text-sm">
          <p>以下 Manager 的运行时不支持审查钩子；启用时服务会再次核对：</p>
          <ul className="space-y-2">{saved.managers_without_hook.map((manager) => <li key={manager.manager_id} className="break-all">
            <a className="text-primary underline" href={`#settings/managers/${encodeURIComponent(manager.manager_id)}`}>{manager.name || manager.manager_id}</a> · {manager.runtime}
          </li>)}</ul>
        </div>}
      </>}
      {review && <a className="inline-flex min-h-11 items-center text-sm text-primary underline" href="#settings/roles/outbound_check">分类器 Profile、提示词与超时</a>}
    </div></CardContent>
  </Card>;
}

export function SecuritySettingsSection() {
  return <div className="min-w-0 space-y-4">
    <a className="inline-flex min-h-11 items-center text-sm text-primary underline" href="#settings/security/audit">查看对外动作审计</a>
    <div className="grid min-w-0 gap-4 xl:grid-cols-2"><SecuritySwitch review /><SecuritySwitch review={false} /></div>
  </div>;
}
