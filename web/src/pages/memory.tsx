import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { LABEL, SELECT } from "@/components/registry-ui";
import {
  memoryConfirm, memoryConfirmMany, memoryDelete, memoryEdit, memoryGet, memoryList, memoryReject, memoryRetry,
  memoryRevive, memorySettingsGet,
  type MemoryBatchItem, type MemoryCandidate, type MemoryChange, type MemoryDetail, type MemorySettingsView,
} from "@/lib/memory";
import { relative } from "@/lib/leads-labels";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";
import { cn } from "@/lib/utils";

const SETTINGS_LINK = "#settings/vault";
const TEXTAREA = "w-full rounded-md border border-input bg-transparent p-3 font-mono text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
const STALE = "打开之后内容改过，请看新版本。";
const DISABLED = "记忆写入没有打开，所以不能做这个操作。";

const KIND_ZH: Record<string, string> = {
  preference: "偏好", feedback: "反馈", decision: "决定", lesson: "经验", fact: "事实", commitment: "承诺",
  person: "人物", project: "项目",
};
const SOURCE_ZH: Record<string, string> = { manager_turn: "对话", task: "任务", lead: "线索" };
const STATE_ZH: Record<string, string> = {
  drafting: "起草中", pending: "待确认", confirmed: "已确认，等待写入", writing: "写入中", written: "已写入",
  rejected: "已驳回", expired: "已过期", conflict: "冲突", blocked: "受阻", dropped: "已丢弃",
};
/** What a blocked or conflicting candidate's reason means, and what the operator can do about it. */
const REASON_ZH: Record<string, string> = {
  base_changed: "要改的笔记在提议之后被改过，原来的整篇替换对不上了。驳回它，等重新提议。",
  anchor_missing: "要追加或替换的那一行在笔记里找不到了。驳回它，等重新提议。",
  target_changed: "写入的目标变了，这份提议对不上。驳回它，等重新提议。",
  vault_problem: "知识库的设置现在有问题。到设置里按「检查」，修好后点重试。",
  not_git: "知识库不在 git 仓库里。修好后点重试。",
  detached_head: "知识库主检出处在分离 HEAD。切回分支后点重试。",
  branch_mismatch: "知识库主检出不在设置里的分支上。切回去后点重试。",
  operation_in_progress: "知识库主检出里有别的 git 操作还没做完。做完后点重试。",
  dirty_target: "目标文件有没提交的改动。先提交或还原它，再点重试。",
  hook_rejected: "git 钩子拒绝了这次提交。处理钩子的问题后点重试。",
  git_failed: "git 命令失败。到设置里按「检查」，修好后点重试。",
  git_timeout: "git 命令超时。稍后点重试。",
  commit_mismatch: "提交的内容和确认的不一致，没有记为已写入。检查仓库后点重试。",
  worktree_failed: "没能建出写入用的 worktree。到设置里按「检查」，修好后点重试。",
  interrupted: "写入被服务重启打断了好几次。检查仓库后点重试。",
  draft_failed: "起草失败了。",
};
const EVENT_ZH: Record<string, string> = {
  propose: "提议", draft: "起草", edit: "编辑", push: "推送", confirm: "确认", reject: "驳回", expire: "过期",
  revive: "恢复", claim: "开始写入", write: "写入完成", conflict: "冲突", block: "受阻", retry: "重试",
  recover: "恢复写入", redraft: "重新起草", drop: "丢弃", merge: "合并",
};

interface Group { id: string; title: string; hint?: string; states: string[] }
const GROUPS: Group[] = [
  { id: "pending", title: "待确认", states: ["pending"] },
  { id: "stuck", title: "受阻与冲突", hint: "这些没有写进知识库，要你处理。", states: ["blocked", "conflict"] },
  { id: "writing", title: "写入中", states: ["confirmed", "writing"] },
  { id: "drafting", title: "起草中", states: ["drafting"] },
  { id: "expired", title: "已过期", hint: "超过期限没人确认。恢复后重新进入待确认。", states: ["expired"] },
  { id: "written", title: "已写入", states: ["written"] },
  { id: "closed", title: "已驳回或丢弃", states: ["rejected", "dropped"] },
];

function flagWords(item: Pick<MemoryCandidate, "untrusted" | "flagged" | "hard_rule">, largeRemoval = false): string[] {
  return [
    ...(item.untrusted ? ["来自消息"] : []),
    ...(item.flagged ? ["有指令"] : []),
    ...(largeRemoval ? ["大段删除"] : []),
    ...(item.hard_rule ? ["硬规则"] : []),
  ];
}

const shortHash = (hash: string | null) => (hash ? hash.slice(0, 8) : "—");

/** The 409 of a confirm or edit: the candidate changed after the page rendered it. */
function staleOrText(err: unknown): string {
  if (err instanceof V1Error && err.status === 409) return `${STALE}${err.message ? `（${err.message}）` : ""}`;
  return errorText(err);
}

export function MemoryPage({ sub = "", onNavigate }: { sub?: string; onNavigate?: (sub: string) => void }) {
  const selected = decodeURIComponent(sub.split("/")[0] ?? "");
  const [view, setView] = useState<MemorySettingsView | null>(null);
  const [viewError, setViewError] = useState("");
  const [filter, setFilter] = useState("open");
  const [items, setItems] = useState<MemoryCandidate[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [results, setResults] = useState<Record<string, { ok: boolean; text: string }>>({});
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchError, setBatchError] = useState("");

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    let live = true;
    memorySettingsGet().then(
      (value) => { if (live) { setView(value); setViewError(""); } },
      (err: unknown) => { if (live) setViewError(errorText(err)); },
    );
    return () => { live = false; };
  }, [attempt]);

  useEffect(() => {
    let live = true;
    memoryList({ state: filter, limit: 100 }).then(
      (page) => {
        if (!live) return;
        setItems(page.candidates);
        setMore(page.more);
        setError("");
        // A row that left the pending group cannot stay ticked.
        setChecked((now) => new Set([...now].filter((id) => page.candidates.some((c) => c.id === id && c.state === "pending"))));
      },
      (err: unknown) => { if (live) { setError(errorText(err)); setItems(null); } },
    );
    return () => { live = false; };
  }, [filter, attempt]);

  // The writer commits after the confirmation, on its own clock: follow the rows it has not finished.
  useEffect(() => {
    if (!items?.some((item) => item.state === "confirmed" || item.state === "writing")) return;
    const timer = setInterval(reload, 3000);
    return () => clearInterval(timer);
  }, [items, reload]);

  const canAct = view?.enabled === true;
  const grouped = useMemo(() => GROUPS.map((group) => ({
    group, rows: (items ?? []).filter((item) => group.states.includes(item.state)),
  })).filter(({ rows }) => rows.length > 0), [items]);
  const other = (items ?? []).filter((item) => !GROUPS.some((g) => g.states.includes(item.state)));

  const toggle = (id: string) => setChecked((now) => {
    const next = new Set(now);
    if (!next.delete(id)) next.add(id);
    return next;
  });

  const confirmChecked = async () => {
    const picked = (items ?? []).filter((item) => checked.has(item.id) && item.content_hash);
    if (!picked.length) return;
    setBatchBusy(true);
    setBatchError("");
    try {
      const answers = await memoryConfirmMany(picked.map((item) => ({ candidate: item.id, content_hash: item.content_hash! })));
      const next: Record<string, { ok: boolean; text: string }> = {};
      answers.forEach((answer: MemoryBatchItem) => {
        next[answer.candidate] = answer.ok
          ? { ok: true, text: "已确认，等待写入" }
          : { ok: false, text: answer.error?.code === "conflict" ? `${STALE}${answer.error.message}` : answer.error?.message ?? "没有确认" };
      });
      setResults(next);
      setChecked(new Set());
      reload();
    } catch (err) {
      setBatchError(staleOrText(err));
    } finally {
      setBatchBusy(false);
    }
  };

  const open = (id: string) => onNavigate?.(id);

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6 max-sm:p-4">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">记忆</h1>
        <p className="text-sm text-muted-foreground">待你确认的、要写进知识库的记忆。确认之前不会写任何文件。</p>
      </div>

      {viewError ? (
        <p role="alert" className="text-sm text-destructive">读不到记忆写入的设置：{viewError}</p>
      ) : view && !view.configured ? (
        <p role="status" data-state="not-configured" className="text-sm">
          记忆写入还没有配置，下面的操作都不可用。<a className="text-primary underline" href={SETTINGS_LINK}>去设置里配置</a>。
        </p>
      ) : view && !view.enabled ? (
        <p role="status" data-state="off" className="text-sm">
          记忆写入已配置但没有打开，下面的操作都不可用。<a className="text-primary underline" href={SETTINGS_LINK}>去设置里打开</a>。
        </p>
      ) : null}
      {view?.problem && (
        <p role="alert" className="break-words text-sm text-destructive">
          记忆写入的设置现在有问题，写入会受阻：{view.problem}。<a className="underline" href={SETTINGS_LINK}>去设置里检查</a>。
        </p>
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
        <div className={cn("min-w-0 space-y-4", selected && "max-lg:hidden")}>
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <label htmlFor="memory-filter" className={LABEL}>显示</label>
              <select id="memory-filter" className={cn(SELECT, "w-44")} value={filter} onChange={(e) => { setItems(null); setFilter(e.target.value); }}>
                <option value="open">还没写完的</option>
                <option value="all">全部</option>
              </select>
            </div>
            <Button type="button" variant="outline" onClick={reload}>刷新</Button>
            <Button type="button" disabled={!canAct || batchBusy || checked.size === 0} onClick={() => void confirmChecked()}
                    title={canAct ? undefined : DISABLED}>
              {batchBusy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
              确认所选{checked.size ? `（${checked.size}）` : ""}
            </Button>
          </div>
          {batchError && <p role="alert" className="break-words text-sm text-destructive">{batchError}</p>}

          {error ? (
            <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
              读不到记忆列表：{error}<Button type="button" size="sm" variant="outline" onClick={reload}>重试</Button>
            </p>
          ) : items === null ? (
            <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />加载中…
            </p>
          ) : items.length === 0 ? (
            <p role="status" className="text-sm text-muted-foreground">
              {filter === "open" ? "现在没有待处理的记忆。" : "还没有任何记忆提议。"}
            </p>
          ) : (
            <>
              {grouped.map(({ group, rows }) => (
                <section key={group.id} aria-labelledby={`memory-group-${group.id}`} className="space-y-2">
                  <h2 id={`memory-group-${group.id}`} className="text-sm font-semibold">
                    {group.title}<span className="ml-2 font-normal text-muted-foreground">{rows.length}</span>
                  </h2>
                  {group.hint && <p className="text-xs text-muted-foreground">{group.hint}</p>}
                  <ul className="divide-y divide-border rounded-lg border border-border bg-card">
                    {rows.map((item) => (
                      <Row key={item.id} item={item} active={item.id === selected} checked={checked.has(item.id)}
                           canAct={canAct} result={results[item.id]} onToggle={() => toggle(item.id)} onOpen={() => open(item.id)} />
                    ))}
                  </ul>
                </section>
              ))}
              {other.length > 0 && (
                <section aria-label="其他" className="space-y-2">
                  <h2 className="text-sm font-semibold">其他</h2>
                  <ul className="divide-y divide-border rounded-lg border border-border bg-card">
                    {other.map((item) => (
                      <Row key={item.id} item={item} active={item.id === selected} checked={false} canAct={false}
                           result={results[item.id]} onToggle={() => {}} onOpen={() => open(item.id)} />
                    ))}
                  </ul>
                </section>
              )}
              {more && <p role="status" className="text-xs text-muted-foreground">还有更早的没有显示。</p>}
            </>
          )}
        </div>

        <div className={cn("min-w-0", !selected && "max-lg:hidden")}>
          {selected ? (
            <Detail key={selected} id={selected} tick={attempt} canAct={canAct} onBack={() => open("")} onChanged={reload} />
          ) : (
            <p className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">选一条看改动、出处和历史。</p>
          )}
        </div>
      </div>
    </div>
  );
}

function Row({ item, active, checked, canAct, result, onToggle, onOpen }: {
  item: MemoryCandidate; active: boolean; checked: boolean; canAct: boolean;
  result?: { ok: boolean; text: string }; onToggle: () => void; onOpen: () => void;
}) {
  const flags = flagWords(item);
  const reason = item.reason ? REASON_ZH[item.reason] : null;
  return (
    <li className={cn("flex items-start gap-3 p-3", active && "bg-primary/5")}>
      {item.state === "pending" && (
        <input type="checkbox" checked={checked} disabled={!canAct} onChange={onToggle}
               aria-label={`选择 ${item.id}`} className="mt-1 h-4 w-4 shrink-0 pointer-coarse:h-6 pointer-coarse:w-6" />
      )}
      <div className="min-w-0 flex-1 space-y-1">
        <button type="button" onClick={onOpen} aria-current={active ? "true" : undefined}
                className="block w-full min-w-0 text-left text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring pointer-coarse:min-h-11">
          <span className="font-mono text-xs text-muted-foreground">{item.id}</span>{" "}
          <Badge variant="outline">{KIND_ZH[item.kind] ?? item.kind}</Badge>{" "}
          <span className="break-words font-medium">{item.title || item.statement}</span>
        </button>
        <p className="break-all text-xs text-muted-foreground">
          {item.note_path ? `${item.note_path} · ` : ""}来自{SOURCE_ZH[item.source_kind] ?? item.source_kind}
          {item.sources_total > 1 ? `等 ${item.sources_total} 处` : ""} · {relative(item.created_at)}
        </p>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={item.state === "blocked" || item.state === "conflict" ? "destructive" : item.state === "pending" ? "warning" : "outline"}>
            {STATE_ZH[item.state] ?? item.state}
          </Badge>
          {flags.map((flag) => <Badge key={flag} variant="warning">{flag}</Badge>)}
        </div>
        {reason && (item.state === "blocked" || item.state === "conflict") && (
          <p className="break-words text-xs">{reason}</p>
        )}
        {result && (
          <p role="status" data-result={result.ok ? "ok" : "failed"}
             className={cn("break-words text-xs", result.ok ? "text-success" : "text-destructive")}>
            {result.ok ? "✓ " : "✗ "}{result.text}
          </p>
        )}
      </div>
    </li>
  );
}

type Pending = "reject" | "delete" | null;

function Detail({ id, tick, canAct, onBack, onChanged }: { id: string; tick: number; canAct: boolean; onBack: () => void; onChanged: () => void }) {
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [ask, setAsk] = useState<Pending>(null);
  const [reason, setReason] = useState("");
  const [drafts, setDrafts] = useState<MemoryChange[] | null>(null);

  useEffect(() => {
    let live = true;
    memoryGet(id).then(
      (value) => { if (live) { setDetail(value); setError(""); } },
      (err: unknown) => { if (live) setError(errorText(err)); },
    );
    return () => { live = false; };
  }, [id, attempt, tick]);

  const failed = (err: unknown) => {
    setActionError(err instanceof V1Error && err.status === 403 ? `${DISABLED}${err.message}` : staleOrText(err));
    // A stale answer means the page shows an old version: read the new one.
    if (err instanceof V1Error && err.status === 409) setAttempt((n) => n + 1);
    onChanged();
  };
  const act = async (text: string, run: () => Promise<MemoryDetail | { deleted: boolean }>) => {
    setBusy(true);
    setActionError("");
    setNotice("");
    try {
      const done = await run();
      if ("deleted" in done) {
        onChanged();
        onBack();
        return;
      }
      setDetail(done);
      setNotice(text);
      setDrafts(null);
      onChanged();
    } catch (err) {
      failed(err);
    } finally {
      setBusy(false);
      setAsk(null);
    }
  };

  if (error) {
    return (
      <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
        读不到 {id}：{error}
        <Button type="button" size="sm" variant="outline" onClick={() => { setError(""); setAttempt((n) => n + 1); }}>重试</Button>
        <Button type="button" size="sm" variant="ghost" onClick={onBack}>返回列表</Button>
      </p>
    );
  }
  if (!detail) {
    return <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />加载中…</p>;
  }

  const state = detail.state;
  const current = detail.current;
  const hash = detail.content_hash ?? current?.content_hash ?? null;
  const flags = flagWords(detail, current?.large_removal);
  const off = !canAct || busy;
  const reasonText = detail.reason ? REASON_ZH[detail.reason] : null;

  const saveDrafts = (confirm: boolean) => {
    if (!drafts || !hash) return;
    void act(confirm ? "已保存并确认，等待写入" : "已保存为新版本", () => memoryEdit(detail.id, hash, drafts, confirm));
  };

  return (
    <article aria-label={detail.id} className="space-y-4 rounded-lg border border-border bg-card p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onBack} className="lg:hidden"><ArrowLeft className="h-4 w-4" aria-hidden="true" />返回列表</Button>
        <h2 className="text-base font-semibold">{detail.id}</h2>
        <Badge variant="outline">{KIND_ZH[detail.kind] ?? detail.kind}</Badge>
        <Badge variant={state === "blocked" || state === "conflict" ? "destructive" : state === "pending" ? "warning" : "outline"}>
          {STATE_ZH[state] ?? state}
        </Badge>
        {flags.map((flag) => <Badge key={flag} variant="warning">{flag}</Badge>)}
        {hash && <span className="ml-auto font-mono text-xs text-muted-foreground" title="内容指纹：确认的是这一版">指纹 {shortHash(hash)}</span>}
      </div>

      {(state === "blocked" || state === "conflict") && (
        <p role="alert" className="break-words text-sm text-destructive">
          {state === "blocked" ? "写入受阻" : "写入冲突"}：{reasonText ?? detail.reason ?? "没有给出原因。"}
        </p>
      )}
      {current?.large_removal && <p role="status" className="text-sm text-warning">这版改动删掉了大段已有内容，确认前请看清下面的差异。</p>}
      {detail.untrusted && <p role="status" className="text-xs text-muted-foreground">这条的出处是别人发来的消息，没有经过你确认。</p>}
      {detail.flagged && <p role="status" className="text-xs text-muted-foreground">出处里带有指令的口气：只在你自己同意时才确认。</p>}

      <section className="space-y-1">
        <h3 className="text-sm font-semibold">{detail.title || "要记的内容"}</h3>
        <p className="whitespace-pre-wrap break-words text-sm">{detail.statement}</p>
        {detail.why && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">理由：{detail.why}</p>}
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">写进知识库的改动</h3>
        {!current ? (
          <p role="status" className="text-sm text-muted-foreground">还没有起草出具体改动。</p>
        ) : drafts ? (
          <EditChanges changes={drafts} onChange={setDrafts} disabled={off} />
        ) : (
          <Changes revision={current} />
        )}
      </section>

      <section className="space-y-1">
        <h3 className="text-sm font-semibold">出处 <span className="font-normal text-muted-foreground">{detail.sources.length}</span></h3>
        {detail.sources.length === 0 ? <p className="text-sm text-muted-foreground">没有记录出处。</p> : (
          <ul className="space-y-2">
            {detail.sources.map((source, index) => (
              <li key={index} className="space-y-0.5 text-sm">
                <p className="text-xs text-muted-foreground">
                  {SOURCE_ZH[source.kind] ?? source.kind}
                  {source.task_id ? <> · <a className="text-primary underline" href={`#tasks/${encodeURIComponent(source.task_id)}`}>任务 {source.task_id}</a></> : null}
                  {source.lead ? ` · ${source.lead}` : ""}
                  {source.channel ? ` · ${source.channel}` : ""}
                  {source.said_at ? ` · ${relative(source.said_at)}` : ""}
                </p>
                {source.quote && <blockquote className="whitespace-pre-wrap break-words border-l-2 border-border pl-3">{source.quote}</blockquote>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-1">
        <h3 className="text-sm font-semibold">历史</h3>
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {detail.events.map((event, index) => (
            <li key={index} className="break-words">
              {relative(event.at)} · {EVENT_ZH[event.action] ?? event.action} · {event.actor === "user" ? "你" : event.actor === "manager" ? "Manager" : "系统"}
              {event.note ? `：${event.note}` : ""}
            </li>
          ))}
        </ul>
      </section>

      {!canAct && <p role="status" className="text-xs text-muted-foreground">记忆写入没有打开：按钮暂时不可用。<a className="text-primary underline" href={SETTINGS_LINK}>去设置</a></p>}
      {actionError && (
        <p role="alert" className="break-words text-sm text-destructive">
          {actionError}{actionError.startsWith(DISABLED) && <> <a className="underline" href={SETTINGS_LINK}>去设置</a></>}
        </p>
      )}
      {notice && <p role="status" className="text-sm text-success">{notice}</p>}

      <div className="flex flex-wrap gap-2">
        {drafts ? (
          <>
            <Button type="button" disabled={off} onClick={() => saveDrafts(false)}>保存</Button>
            <Button type="button" disabled={off} onClick={() => saveDrafts(true)}>保存并写入</Button>
            <Button type="button" variant="outline" disabled={busy} onClick={() => setDrafts(null)}>放弃修改</Button>
          </>
        ) : (
          <>
            {state === "pending" && hash && (
              <Button type="button" disabled={off} onClick={() => void act("已确认，等待写入", () => memoryConfirm(detail.id, hash))}>确认写入</Button>
            )}
            {state === "pending" && current && (
              <Button type="button" variant="outline" disabled={off} onClick={() => setDrafts(current.changes.map((c) => ({ ...c })))}>编辑</Button>
            )}
            {state === "blocked" && (
              <Button type="button" disabled={off} onClick={() => void act("已重试，等待写入", () => memoryRetry(detail.id))}>重试</Button>
            )}
            {state === "expired" && (
              <Button type="button" disabled={off} onClick={() => void act("已恢复", () => memoryRevive(detail.id))}>恢复</Button>
            )}
            {["pending", "blocked", "conflict", "expired"].includes(state) && (
              <Button type="button" variant="outline" disabled={off} onClick={() => { setReason(""); setAsk("reject"); }}>驳回</Button>
            )}
            {state !== "writing" && (
              <Button type="button" variant="destructive" disabled={off} onClick={() => setAsk("delete")}>删除</Button>
            )}
          </>
        )}
      </div>

      {ask === "reject" && (
        <ConfirmDialog title={`驳回 ${detail.id}`} onClose={() => setAsk(null)}>
          <label htmlFor="memory-reject-reason" className={LABEL}>原因（可不填）</label>
          <Input id="memory-reject-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setAsk(null)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => void act("已驳回", () => memoryReject(detail.id, reason.trim() || undefined))}>驳回</Button>
          </div>
        </ConfirmDialog>
      )}
      {ask === "delete" && (
        <ConfirmDialog title={`删除 ${detail.id}`} onClose={() => setAsk(null)}>
          <p className="text-sm">删除后这条记录和它的历史就没有了，只留下一个防止它再被提议的指纹。已经写进知识库的笔记不会动。</p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setAsk(null)}>取消</Button>
            <Button type="button" variant="destructive" disabled={busy} onClick={() => void act("已删除", () => memoryDelete(detail.id))}>删除</Button>
          </div>
        </ConfirmDialog>
      )}
    </article>
  );
}

const OP_ZH: Record<string, string> = {
  create_note: "新建笔记", replace_note: "替换整篇笔记", append_line: "追加一行", replace_line: "改一行",
};

/** The server's own unified diff per file; without one, the change as written. */
function Changes({ revision }: { revision: NonNullable<MemoryDetail["current"]> }) {
  const diffs = revision.diffs ?? {};
  const paths = [...new Set(revision.changes.map((change) => change.path))];
  return (
    <div className="space-y-3">
      {revision.diffs_problem && <p role="status" className="break-words text-sm text-warning">看不到和知识库现状的差异：{revision.diffs_problem}</p>}
      {paths.map((path) => {
        const stats = revision.diff_stats[path];
        const ops = revision.changes.filter((change) => change.path === path).map((change) => OP_ZH[change.op] ?? change.op);
        return (
          <div key={path} className="space-y-1">
            <p className="break-all text-xs"><span className="font-mono">{path}</span>
              <span className="text-muted-foreground"> · {ops.join("、")}{stats ? ` · +${stats.added ?? 0} −${stats.removed ?? 0}` : ""}</span></p>
            {diffs[path] ? <DiffText text={diffs[path]} /> : (
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border p-2 font-mono text-xs">
                {revision.changes.filter((change) => change.path === path).map(changeText).join("\n")}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}

function changeText(change: MemoryChange): string {
  switch (change.op) {
    case "create_note": case "replace_note": return change.content;
    case "append_line": return `${change.under ? `在「${change.under}」下追加：\n` : "追加：\n"}${change.line}`;
    case "replace_line": return `- ${change.old}\n+ ${change.new}`;
  }
}

function DiffText({ text }: { text: string }) {
  return (
    <pre className="max-h-80 overflow-auto rounded-md border border-border p-2 font-mono text-xs" aria-label="差异">
      {text.split("\n").map((line, index) => (
        <div key={index} className={cn("whitespace-pre-wrap break-words",
          line.startsWith("+") && !line.startsWith("+++") && "bg-success/10 text-success",
          line.startsWith("-") && !line.startsWith("---") && "bg-destructive/10 text-destructive",
          line.startsWith("@@") && "text-muted-foreground")}>{line || " "}</div>
      ))}
    </pre>
  );
}

/** Editing changes the text only; the operation, path and anchors stay as drafted. */
function EditChanges({ changes, onChange, disabled }: { changes: MemoryChange[]; onChange: (next: MemoryChange[]) => void; disabled: boolean }) {
  const set = (index: number, change: MemoryChange) => onChange(changes.map((c, i) => (i === index ? change : c)));
  return (
    <div className="space-y-3">
      {changes.map((change, index) => {
        const id = `memory-edit-${index}`;
        return (
          <div key={index} className="space-y-1.5">
            <label htmlFor={id} className={LABEL}>{OP_ZH[change.op] ?? change.op} · <span className="font-mono">{change.path}</span></label>
            {change.op === "create_note" || change.op === "replace_note" ? (
              <textarea id={id} rows={10} className={TEXTAREA} disabled={disabled} value={change.content}
                        onChange={(e) => set(index, { ...change, content: e.target.value })} />
            ) : change.op === "append_line" ? (
              <textarea id={id} rows={2} className={TEXTAREA} disabled={disabled} value={change.line}
                        onChange={(e) => set(index, { ...change, line: e.target.value })} />
            ) : (
              <textarea id={id} rows={2} className={TEXTAREA} disabled={disabled} value={change.new}
                        onChange={(e) => set(index, { ...change, new: e.target.value })} />
            )}
          </div>
        );
      })}
    </div>
  );
}
