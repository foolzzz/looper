import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { fetchOutboundReviews, type ReviewPage, type ReviewRow } from "@/lib/api-security";
import { fetchLlmRecord } from "@/lib/api";
import { V1Error } from "@/lib/v1";

function Review({ row }: { row: ReviewRow }) {
  const [expanded, setExpanded] = useState(false);
  const [runAgent, setRunAgent] = useState<string | null>(null);
  const [runError, setRunError] = useState("");
  const recordId = row.record_id === null ? null : Number(row.record_id);
  const safeRecordId = recordId !== null && Number.isSafeInteger(recordId) && recordId > 0 ? recordId : null;
  useEffect(() => {
    if (!expanded || safeRecordId === null) return;
    let current = true;
    setRunError("");
    void fetchLlmRecord(safeRecordId).then((record) => {
      if (current) { if (record) setRunAgent(record.agent_id); else setRunError("运行记录已不可用"); }
    }).catch(() => { if (current) setRunError("运行记录暂时不可读"); });
    return () => { current = false; };
  }, [expanded, safeRecordId]);
  const history = (agent: string) => `#history/${encodeURIComponent(agent)}${row.task_id ? `?task=${encodeURIComponent(row.task_id)}` : ""}`;
  return <article className="min-w-0 rounded-lg border border-border p-4 [overflow-wrap:anywhere]">
    <dl className="grid min-w-0 gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
      <div><dt className="text-muted-foreground">时间</dt><dd><time dateTime={row.created_at}>{new Date(row.created_at).toLocaleString()}</time></dd></div>
      <div><dt className="text-muted-foreground">Agent</dt><dd className="font-mono">{row.agent_id}</dd></div>
      <div><dt className="text-muted-foreground">工具 / 目标</dt><dd>{row.tool}{row.action ? ` · ${row.action}` : ""}<br />{row.target_kind} · {row.target}</dd></div>
      <div><dt className="text-muted-foreground">判定 / 理由</dt><dd><Badge variant={row.verdict === "block" ? "destructive" : "outline"}>{row.verdict === "block" ? "拦截" : "放行"}</Badge><p className="mt-1 whitespace-pre-wrap">{row.reason}</p></dd></div>
      <div><dt className="text-muted-foreground">花费</dt><dd>{row.cost_usd === null ? "未知" : `$${row.cost_usd.toFixed(4)}`}</dd></div>
      <div><dt className="text-muted-foreground">耗时</dt><dd>{row.latency_ms === null ? "未记录" : `${row.latency_ms} ms`}</dd></div>
    </dl>
    <Button className="mt-3" variant="ghost" aria-expanded={expanded} aria-controls={`review-${row.id}`} onClick={() => setExpanded(!expanded)}>{expanded ? "收起详情" : "展开详情"}</Button>
    {expanded && <div id={`review-${row.id}`} className="mt-3 space-y-2 border-t border-border pt-3 text-sm">
      <p className="whitespace-pre-wrap">摘要：{row.summary}</p><p>信号：{row.signals.length ? row.signals.join("、") : "无"}</p>
      <p>失败类型：{row.failure ?? "无"}</p><p>来源：{row.source} · 动作级别：{row.level} · 记录：{row.id}</p>
      {row.record_id && <p>分类器运行编号：{row.record_id}{safeRecordId === null ? "（超出现有运行接口支持的编号范围）" : runError ? `（${runError}）` : !runAgent ? "（正在读取运行链接…）" : ""}</p>}
      <div className="flex flex-wrap gap-4">
        {runAgent && <a className="inline-flex min-h-11 items-center text-primary underline" href={history(runAgent)}>分类器运行</a>}
        {row.parent_record_id && <a className="inline-flex min-h-11 items-center text-primary underline" href={history(row.agent_id)}>发起者运行</a>}
        {row.task_id && <a className="inline-flex min-h-11 items-center text-primary underline" href={`#tasks/${encodeURIComponent(row.task_id)}`}>任务</a>}
      </div>
    </div>}
  </article>;
}

export function OutboundReviewAudit() {
  const [initial] = useState(() => window.history.state?.outboundReviewAudit as {
    task: string; blocked: boolean; cursors: (string | undefined)[]; scrollTop?: number;
  } | undefined);
  const [taskDraft, setTaskDraft] = useState(initial?.task ?? "");
  const [task, setTask] = useState(initial?.task ?? "");
  const [blocked, setBlocked] = useState(initial?.blocked ?? false);
  const [cursors, setCursors] = useState<(string | undefined)[]>(initial?.cursors ?? [undefined]);
  const [page, setPage] = useState<ReviewPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [revision, setRevision] = useState(0);
  const sequence = useRef(0);
  const area = useRef<HTMLDivElement>(null);
  const restored = useRef(false);
  const cursor = cursors[cursors.length - 1];
  function remember(scrollTop?: number) {
    window.history.replaceState({ ...window.history.state, outboundReviewAudit: { task, blocked, cursors, scrollTop } }, "");
  }
  useEffect(() => {
    window.history.replaceState({ ...window.history.state, outboundReviewAudit: { task, blocked, cursors } }, "");
  }, [task, blocked, cursors]);
  useEffect(() => {
    if (page && !restored.current) {
      const scroll = area.current?.closest<HTMLElement>(".overflow-y-auto");
      if (scroll && initial?.scrollTop !== undefined) scroll.scrollTop = initial.scrollTop;
      restored.current = true;
    }
  }, [page, initial]);
  useEffect(() => {
    const current = ++sequence.current, controller = new AbortController();
    setLoading(true); setError("");
    void fetchOutboundReviews({ limit: 50, ...(task ? { task_id: task } : {}), ...(blocked ? { verdict: "block" } : {}), ...(cursor ? { cursor } : {}) }, controller.signal)
      .then((value) => { if (current === sequence.current && !controller.signal.aborted) { setPage(value); setForbidden(false); } })
      .catch((reason: unknown) => {
        if (current !== sequence.current || controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        setForbidden(reason instanceof V1Error && reason.status === 403);
      }).finally(() => { if (current === sequence.current && !controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [task, blocked, cursor, revision]);
  function filterTask() { setTask(taskDraft.trim()); setCursors([undefined]); setPage(null); }
  return <div ref={area} className="min-w-0" onClickCapture={(event) => {
    if ((event.target as HTMLElement).closest("a")) remember(area.current?.closest<HTMLElement>(".overflow-y-auto")?.scrollTop);
  }}><Card className="min-w-0">
    <CardHeader><a className="inline-flex min-h-11 items-center text-sm text-primary underline" href="#settings/security">返回安全设置</a><CardTitle>对外动作审计</CardTitle></CardHeader>
    <CardContent className="min-w-0 space-y-4">
      <form className="flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); filterTask(); }}>
        <label className="min-w-0 basis-full space-y-1 text-sm sm:flex-1">任务 ID<Input className="pointer-coarse:text-base" aria-label="任务 ID" maxLength={200} value={taskDraft} onChange={(event) => setTaskDraft(event.target.value)} placeholder="全部任务" /></label>
        <Button type="submit">筛选</Button>
        <label className="inline-flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" checked={blocked}
          onChange={(event) => { setBlocked(event.target.checked); setCursors([undefined]); setPage(null); }} />只看拦截</label>
        <Button type="button" variant="outline" disabled={loading} onClick={() => setRevision((n) => n + 1)}>刷新</Button>
      </form>
      {forbidden ? <p role="alert" className="text-sm">仅操作者可查看审计记录。</p> : <>
        {loading && <p className="min-h-6 text-sm text-muted-foreground" role="status">正在读取审计记录…</p>}
        {error && <div className="space-y-2"><p role="alert" className="break-words text-sm text-destructive">审计记录不可读：{error}{page ? "；以下数据未刷新。" : ""}</p>
          <Button variant="outline" onClick={() => setRevision((n) => n + 1)}>重试</Button></div>}
        {page && <div className="min-w-0 space-y-3" aria-busy={loading}>{page.items.map((row) => <Review key={row.id} row={row} />)}</div>}
        {!loading && !error && page?.items.length === 0 && <p className="py-6 text-sm text-muted-foreground">没有匹配的审查记录</p>}
        <div className="flex flex-wrap gap-2"><Button variant="outline" aria-label="返回上一页" disabled={loading || !!error || cursors.length === 1} onClick={() => setCursors((values) => values.slice(0, -1))}>返回</Button>
          <Button variant="outline" disabled={loading || !!error || !page?.next_cursor} onClick={() => setCursors((values) => [...values, page!.next_cursor!])}>更早</Button></div>
      </>}
    </CardContent>
  </Card></div>;
}
