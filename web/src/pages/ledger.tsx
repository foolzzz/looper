import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { AlertTriangle, BookOpen, FileText, GitBranch, Link as LinkIcon, ListTodo, Plus, Terminal, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ledgerAction, ledgerAdd, ledgerGet, ledgerPage, type LedgerAction, type LedgerAdd, type LedgerAmend,
  type LedgerDetail, type LedgerEvent, type LedgerItem, type LedgerKind, type LedgerPageRequest,
  type LedgerPageResult, type LedgerRef, type LedgerSection, type LedgerStatus, type LedgerVia,
} from "@/lib/api-ledger";
import { useLedger } from "@/lib/use-ledger";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";
const FIELD = "w-full min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm pointer-coarse:min-h-11 pointer-coarse:text-base focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50";
const LINK = "rounded-sm text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:items-center";
const KINDS: Record<LedgerKind, string> = { pending: "待办", decision: "决定", work_item: "工作项" };
const STATUSES: Record<LedgerStatus, string> = { open: "未结", done: "已完成", cancelled: "已撤销", expired: "已过期", superseded: "已被取代" };
const VIA: Record<LedgerVia, string> = { api: "操作者接口", cli: "命令行", dashboard: "控制台聊天", lark: "飞书", telegram: "Telegram", system: "系统登记" };
const TITLES: Record<LedgerSection, string> = { attention: "要你拍板", work: "进行中", recent_decisions: "最近决定", all: "筛选结果" };
const ownerValid = (owner: string) => {
  if (owner === "user") return true;
  const separator = owner.indexOf(":");
  const kind = owner.slice(0, separator), ref = owner.slice(separator + 1);
  return ["manager", "task", "chat"].includes(kind) && Boolean(ref.trim())
    && (kind === "chat" || /^[\w-]+$/.test(ref));
};
const ownerFilterValid = (owner: string) => ["user", "manager", "task", "chat"].includes(owner) || ownerValid(owner);
const statusText = (item: LedgerItem) => item.status === "open" ? item.kind === "decision" ? item.assurance === "user" ? "有效" : "待本人核实" : item.kind === "work_item" ? "进行中" : "待你确认" : item.status ? STATUSES[item.status] : "来源状态未知";
const dateText = (date: string) => new Date(date).toLocaleString();

function normalizedQuery(raw: string) {
  const old = new URLSearchParams(raw), query = new URLSearchParams();
  const status = old.get("status"), kind = old.get("kind"), owner = old.get("owner")?.trim(), project = old.get("project")?.trim();
  if (status && (status === "all" || Object.hasOwn(STATUSES, status))) query.set("status", status);
  if (kind && Object.hasOwn(KINDS, kind)) query.set("kind", kind);
  if (owner && ownerFilterValid(owner)) query.set("owner", owner);
  if (project && project.length <= 200 && /^[\w-]+$/.test(project)) query.set("project", project);
  const item = old.get("item");
  if (item && /^(?:L-)?\d+$/i.test(item)) {
    const number = Number(item.replace(/^L-/i, ""));
    if (Number.isSafeInteger(number) && number > 0) query.set("item", `L-${String(number).padStart(4, "0")}`);
  }
  return query;
}

interface SectionState { items: LedgerItem[] | null; more: number; cursor: string | null; busy: boolean; error: string; forbidden: boolean; }
const unread = (): SectionState => ({ items: null, more: 0, cursor: null, busy: true, error: "", forbidden: false });

/** The page fetches its own sections; the sidebar's one-item read supplies only a badge. */

export function LedgerPage({ query = "", onQuery }: { query?: string; onQuery: (query: string, replace?: boolean) => void; }) {
  const ledger = useLedger();
  const params = useMemo(() => normalizedQuery(query), [query]);
  const canonical = params.toString(), itemId = params.get("item");
  const filters = new URLSearchParams(params);
  filters.delete("item");
  const filterKey = filters.toString(), filtered = Boolean(filterKey);
  const sections: LedgerSection[] = filtered ? ["all"] : ["attention", "work", "recent_decisions"];
  const [groups, setGroups] = useState<Partial<Record<LedgerSection, SectionState>>>({});
  const [missing, setMissing] = useState<LedgerPageResult["missing_operator_ids"]>([]);
  const [reload, setReload] = useState(0), [adding, setAdding] = useState(false);
  const generation = useRef(0), lastFilters = useRef<string | null>(null), controller = useRef<AbortController | null>(null);
  const origin = useRef<HTMLElement | null>(null), wasOpen = useRef(false);
  const forbidden = Object.values(groups).some((group) => group?.forbidden) || ledger.error === "无权访问账本";
  useEffect(() => {
    if (query.replace(/^\?/, "") !== canonical) onQuery(canonical, true);
  }, [canonical, query, onQuery]);
  useEffect(() => {
    if (!itemId && wasOpen.current) origin.current?.focus();
    wasOpen.current = Boolean(itemId);
  }, [itemId]);
  const request = useCallback((section: LedgerSection): LedgerPageRequest => {
    const p = new URLSearchParams(filterKey);
    return {
      section, limit: 10, ...(filtered ? { status: (p.get("status") || "open") as LedgerStatus | "all" } : {}),
      ...(p.has("kind") ? { kind: p.get("kind") as LedgerKind } : {}), ...(p.has("owner") ? { owner: p.get("owner")! } : {}),
      ...(p.has("project") ? { project_id: p.get("project")! } : {})
    };
  }, [filterKey, filtered]);
  useEffect(() => {
    const token = ++generation.current;
    controller.current?.abort();
    if (ledger.enabled !== true) {
      setGroups({});
      return;
    }
    const abort = new AbortController();
    controller.current = abort;
    const changed = lastFilters.current !== filterKey;
    lastFilters.current = filterKey;
    const list: LedgerSection[] = filtered ? ["all"] : ["attention", "work", "recent_decisions"];
    setGroups((old) => Object.fromEntries(list.map((section) => [section, changed || !old[section] ? unread() : { ...old[section], busy: true, error: "" }])));
    for (const section of list) {
      void ledgerPage(request(section), abort.signal).then((result) => {
        if (token !== generation.current || abort.signal.aborted) return;
        setMissing(result.missing_operator_ids);
        setGroups((old) => ({ ...old, [section]: { items: result.items, more: result.more, cursor: result.next_cursor, busy: false, error: "", forbidden: false } }));
      }).catch((err) => {
        if (token !== generation.current || abort.signal.aborted) return;
        setGroups((old) => ({
          ...old, [section]: {
            ...(old[section] || unread()), busy: false,
            error: errorText(err), forbidden: err instanceof V1Error && err.status === 403
          }
        }));
      });
    }
    return () => {
      abort.abort();
    };
  }, [ledger.enabled, ledger.revision, filterKey, filtered, reload, request]);
  async function more(section: LedgerSection) {
    const group = groups[section];
    if (!group?.cursor || group.busy || ledger.enabled !== true) return;
    const token = generation.current, signal = controller.current?.signal;
    setGroups((old) => ({ ...old, [section]: { ...group, busy: true, error: "" } }));
    try {
      const result = await ledgerPage({ ...request(section), cursor: group.cursor }, signal);
      if (token !== generation.current || signal?.aborted) return;
      const entries = new Map((group.items || []).map((item) => [item.id, item]));
      result.items.forEach((item) => entries.set(item.id, item));
      setGroups((old) => ({
        ...old, [section]: {
          items: [...entries.values()], more: result.more,
          cursor: result.next_cursor, busy: false, error: "", forbidden: false
        }
      }));
    } catch (err) {
      if (token === generation.current && !signal?.aborted) setGroups((old) => ({
        ...old, [section]: {
          ...group, busy: false,
          error: errorText(err), forbidden: err instanceof V1Error && err.status === 403
        }
      }));
    }
  }
  function updateFilter(key: string, value: string) {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    next.delete("item");
    onQuery(next.toString());
  }
  function open(item: LedgerItem, target?: HTMLElement) {
    if (target) origin.current = target;
    const next = new URLSearchParams(params);
    next.set("item", item.id);
    onQuery(next.toString());
  }
  function close() {
    const next = new URLSearchParams(params);
    next.delete("item");
    onQuery(next.toString());
  }
  const loaded = sections.every((section) => groups[section]?.items != null);
  const empty = loaded && sections.every((section) => groups[section]?.items?.length === 0 && !groups[section]?.error);
  return <div className="relative h-full min-w-0 overflow-y-auto p-4 sm:p-6 [overflow-wrap:anywhere]">
    <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
      <h1 className="flex items-center gap-2 text-xl font-semibold">
        <BookOpen className="h-5 w-5" aria-hidden="true" />
        账本
      </h1>
      {ledger.enabled === true && !forbidden && <Button onClick={() => {
        origin.current = document.activeElement as HTMLElement;
        setAdding(true);
      }}>
        <Plus className="h-4 w-4" aria-hidden="true" />
        新增事项
      </Button>}
    </div>
    {ledger.enabled === null ? <p role={ledger.error ? "alert" : "status"}>
      {ledger.error ? `读取失败：${ledger.error}` : "读取中…"}
      {ledger.error && <Button variant="outline" onClick={ledger.refresh}>重试</Button>}
    </p>
      : !ledger.enabled ? <p>
        账本尚未启用。请在
        <a href="#settings/chat" className={LINK}>Settings → Chat</a>
        打开账本。
      </p>
        : <>
          <div className="mb-4 flex flex-wrap gap-3" role="group" aria-label="账本筛选">
            <label className="min-w-32 flex-1 text-sm">
              状态
              <select className={FIELD} aria-label="状态筛选" value={params.get("status") || ""} onChange={(e) => updateFilter("status", e.target.value)}>
                <option value="">默认未结</option>
                <option value="all">全部状态</option>
                {Object.entries(STATUSES).map(([key, label]) => <option key={key} value={key}>
                  {label}
                </option>)}
              </select>
            </label>
            <label className="min-w-32 flex-1 text-sm">
              类型
              <select className={FIELD} aria-label="类型筛选" value={params.get("kind") || ""} onChange={(e) => updateFilter("kind", e.target.value)}>
                <option value="">全部类型</option>
                {Object.entries(KINDS).map(([key, label]) => <option key={key} value={key}>
                  {label}
                </option>)}
              </select>
            </label>
            <label className="min-w-36 flex-1 text-sm">
              负责人
              <Input
                className="pointer-coarse:text-base"
                aria-label="负责人筛选"
                placeholder="user / task:编号"
                key={params.get("owner") || ""}
                defaultValue={params.get("owner") || ""}
                onBlur={(e) => updateFilter("owner", e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.currentTarget.blur();
                }}
              />
            </label>
            <label className="min-w-32 flex-1 text-sm">
              项目
              <Input
                className="pointer-coarse:text-base"
                aria-label="项目筛选"
                placeholder="项目 ID"
                key={params.get("project") || ""}
                defaultValue={params.get("project") || ""}
                onBlur={(e) => updateFilter("project", e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") e.currentTarget.blur();
                }}
              />
            </label>
            {filtered && <Button variant="outline" className="self-end" onClick={() => onQuery(itemId ? `item=${itemId}` : "")}>清除筛选</Button>}
          </div>
          {missing.length > 0 && <div role="status" className="mb-4 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
            <AlertTriangle className="mr-2 inline h-4 w-4" aria-hidden="true" />
            以下机器人未填「我的 ID」：
            {missing.map((bot) => bot.name).join("、")}
            。这些私聊的决定会记为转述。请在
            <a href="#settings/channels" className={LINK}>Settings → Channels</a>
            填写。
          </div>}
          {forbidden ? <p role="alert">无权访问账本。请使用操作者会话。</p> : <>
            {empty && <p className="mb-4 text-sm text-muted-foreground">
              {filtered ? "没有符合筛选的事项" : "没有未结的事项"}
            </p>}
            <div className="space-y-5">
              {sections.map((section) => {
                const group = groups[section] || unread();
                return <section key={section} aria-label={TITLES[section]} className="space-y-2">
                  <h2 className="text-base font-semibold">
                    {TITLES[section]}
                  </h2>
                  {group.items?.map((item) => <button
                    data-ledger-row
                    key={item.id}
                    onClick={(e) => open(item, e.currentTarget)}
                    className="block min-h-11 w-full min-w-0 rounded-md border border-border bg-card p-3 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none"
                  >
                    <ItemSummary item={item} />
                  </button>)}
                  {group.busy && <div role="status" className="rounded-md border border-border p-4 text-sm text-muted-foreground">读取中…</div>}
                  {group.error && <p role="alert" className="text-sm text-destructive">
                    读取失败：
                    {group.error}
                    {group.items?.length ? "已保留已读事项。" : ""}
                    <Button variant="outline" size="sm" onClick={() => group.cursor && group.items?.length ? void more(section) : setReload((n) => n + 1)}>重试</Button>
                  </p>}
                  {group.more > 0 && <Button variant="outline" disabled={group.busy} onClick={() => void more(section)}>
                    {`另有 ${group.more} 项`}
                  </Button>}
                </section>;
              })}
            </div>
          </>}
        </>}
    {ledger.enabled === true && itemId && !forbidden && <LedgerDrawer key={itemId} itemId={itemId} onClose={close} onChanged={ledger.changed} />}
    {ledger.enabled === true && adding && !forbidden && <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-3"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          setAdding(false);
          origin.current?.focus();
        }
      }}
    >
      <div role="dialog" aria-modal="true" aria-label="新增事项" onKeyDown={trapFocus} className="max-h-full w-full max-w-xl overflow-y-auto rounded-lg border border-border bg-popover p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold">新增事项</h2>
          <Button variant="ghost" size="icon" aria-label="关闭新增" onClick={() => {
            setAdding(false);
            origin.current?.focus();
          }}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <ItemForm
          onCancel={() => {
            setAdding(false);
            origin.current?.focus();
          }}
          onSaved={(item) => {
            setAdding(false);
            ledger.changed();
            open(item);
          }}
        />
      </div>
    </div>}
  </div>;
}

function ItemSummary({ item }: { item: LedgerItem; }) {
  const source = item.source?.kind;
  const Icon = source === "gate" ? GitBranch : source === "task" ? ListTodo : item.added_via === "cli" ? Terminal : FileText;
  const sourceLabel = source === "gate" ? "关卡" : source === "task" ? "任务" : source === "confirmation" ? "确认" : item.added_via === "api" && item.assurance !== "user" ? "API 转述" : item.added_via ? VIA[item.added_via] : "手工";
  const overdueDays = item.due_at && item.overdue ? Math.max(1, Math.ceil((Date.now() - new Date(item.due_at).getTime()) / 86_400_000)) : 0;
  return <><div className="flex min-w-0 flex-wrap items-start gap-x-3 gap-y-1">
    <span className="font-mono text-xs text-muted-foreground">
      {item.id}
    </span>
    <span className="min-w-0 flex-1 font-medium [overflow-wrap:anywhere]">
      {item.title}
    </span>
    <Badge variant={item.status === "open" ? "default" : "outline"}>
      {statusText(item)}
    </Badge>
  </div><div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
      <span className="inline-flex items-center gap-1">
        <Icon className="h-3 w-3" aria-hidden="true" />
        {sourceLabel}
      </span>
      <span>
        {KINDS[item.kind]}
      </span>
      {item.assurance === "relayed" && <span>
        转述
        {item.kind === "decision" ? "，待本人核实" : ""}
      </span>}
      <span>
        负责人：
        {item.owner === "user" ? "你本人" : item.owner}
      </span>
      {item.project_id && <span>
        项目：
        {item.project_id}
      </span>}
      {item.due_at && <span className={overdueDays ? "text-warning" : ""}>
        {overdueDays ? `逾期 ${overdueDays} 天` : `到期 ${dateText(item.due_at)}`}
      </span>}
    </div></>;
}

function trapFocus(event: React.KeyboardEvent<HTMLElement>) {
  if (event.key !== "Tab") return;
  const targets = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')];
  const first = targets[0], last = targets.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  }
  else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}

function LedgerDrawer({ itemId, onClose, onChanged }: { itemId: string; onClose: () => void; onChanged: () => void; }) {
  const [detail, setDetail] = useState<LedgerDetail | null>(null), [readError, setReadError] = useState("");
  const [mode, setMode] = useState<"edit" | "note" | null>(null), [note, setNote] = useState("");
  const [busy, setBusy] = useState(false), [writeError, setWriteError] = useState(""), [denied, setDenied] = useState(false), [cancel, setCancel] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null), sequence = useRef(0), alive = useRef(true), submitting = useRef(false);
  const titleId = useId();
  const read = useCallback(async (signal?: AbortSignal) => {
    const token = ++sequence.current;
    try {
      const result = await ledgerGet(itemId, signal);
      if (alive.current && token === sequence.current && !signal?.aborted) {
        setDetail(result);
        setReadError("");
      }
    }
    catch (err) {
      if (alive.current && token === sequence.current && !signal?.aborted) {
        setReadError(err instanceof V1Error && err.status === 404 ? "未找到此事项" : err instanceof V1Error && err.status === 403 ? "无权访问账本" : `读取失败：${errorText(err)}`);
        if (err instanceof V1Error && err.status === 403) setDenied(true);
      }
    }
  }, [itemId]);
  useEffect(() => {
    alive.current = true;
    const abort = new AbortController();
    void read(abort.signal);
    closeRef.current?.focus();
    return () => {
      alive.current = false;
      ++sequence.current;
      abort.abort();
    };
  }, [read]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, [onClose]);
  async function action(args: LedgerAction) {
    if (submitting.current || denied) return;
    submitting.current = true;
    setBusy(true);
    setWriteError("");
    try {
      await ledgerAction(args);
      await read();
      onChanged();
      if (alive.current) {
        setMode(null);
        setNote("");
        setCancel(false);
      }
    }
    catch (err) {
      if (!alive.current) return;
      setWriteError(writeFailure(err));
      if (err instanceof V1Error && err.status === 403) setDenied(true);
      if (err instanceof V1Error && err.status === 409) await read();
    }
    finally {
      submitting.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const item = detail?.item;
  return <div className="fixed inset-0 z-40 flex justify-end bg-background/60">
    <aside
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onKeyDown={trapFocus}
      className="h-dvh w-full min-w-0 max-w-xl overflow-y-auto border-l border-border bg-popover p-4 sm:p-6 [overflow-wrap:anywhere]"
    >
      <div className="mb-4 flex items-center justify-between gap-2">
        <h2 id={titleId} className="text-lg font-semibold">
          {itemId}
          详情
        </h2>
        <Button ref={closeRef} variant="ghost" size="icon" aria-label="关闭详情" onClick={onClose}>
          <X className="h-4 w-4" aria-hidden="true" />
        </Button>
      </div>
      {!detail && !readError && <p role="status">读取中…</p>}
      {readError && <p role="alert">
        {readError}
        <Button variant="outline" onClick={() => void read()}>重试</Button>
      </p>}
      {item && !denied && <div className="space-y-5">
        <ItemSummary item={item} />
        {item.detail && <p className="whitespace-pre-wrap text-sm">
          {item.detail}
        </p>}
        {item.source && (item.source.href?.startsWith("#tasks/") || item.source.href?.startsWith("#chat?") ? <a className={LINK} href={item.source.href}>去处理</a> : <p className="text-sm text-muted-foreground">请回原提问会话处理；来源未提供跳转。</p>)}
        {item.refs.length > 0 && <ul className="space-y-2 text-sm">
          {item.refs.map((ref, n) => <li key={n}>
            <LinkIcon className="mr-1 inline h-3 w-3" aria-hidden="true" />
            {safeRef(ref) ? <a href={safeRef(ref)!} target="_blank" rel="noopener noreferrer" className={LINK}>
              {ref.label || ref.ref}
            </a> : <span>
              {ref.label ? `${ref.label} · ` : ""}
              {ref.ref}
            </span>}
          </li>)}
        </ul>}
        <div className="flex flex-wrap gap-2" aria-label="事项操作">
          {!item.source && item.status === "open" && item.kind === "decision" && item.assurance === "relayed" && <Button disabled={busy || mode !== null} onClick={() => void action({ item: item.id, action: "confirm" })}>确认</Button>}
          {!item.source && item.status === "open" && item.kind !== "decision" && <Button disabled={busy || mode !== null} onClick={() => void action({ item: item.id, action: "close" })}>完成</Button>}
          {!item.source && item.status === "open" && <Button variant="outline" disabled={busy || mode !== null} onClick={() => setCancel(true)}>撤销</Button>}
          {!item.source && item.status !== "open" && <Button variant="outline" disabled={busy || mode !== null} onClick={() => void action({ item: item.id, action: "reopen" })}>重新打开</Button>}
          <Button variant="outline" disabled={busy} onClick={() => {
            setMode("edit");
            setWriteError("");
          }}>
            {item.source ? "编辑到期、负责人" : "编辑"}
          </Button>
          <Button variant="outline" disabled={busy} onClick={() => {
            setMode("note");
            setWriteError("");
          }}>备注</Button>
        </div>
        {cancel && <div className="space-y-2 rounded-md border border-warning/40 p-3">
          <p className="text-sm">确定撤销此事项？历史记录会保留。</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="destructive" disabled={busy || mode !== null} onClick={() => void action({ item: item.id, action: "cancel" })}>确认撤销</Button>
            <Button variant="outline" disabled={busy} onClick={() => setCancel(false)}>保留事项</Button>
          </div>
        </div>}
        {writeError && <p role="alert" className="text-sm text-destructive">
          {writeError}
        </p>}
        {mode === "edit" && <ItemForm
          item={item}
          onCancel={() => setMode(null)}
          onConflict={() => read()}
          onSaved={() => {
            setMode(null);
            void read();
            onChanged();
          }}
        />}
        {mode === "note" && <form
          onSubmit={(e) => {
            e.preventDefault();
            if (note.trim()) void action({ item: item.id, action: "note", note: note.trim() });
          }}
          className="space-y-2"
        >
          <label className="block text-sm">
            备注
            <textarea autoFocus aria-label="备注内容" className={FIELD} value={note} maxLength={2000} required onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={busy || !note.trim()}>保存备注</Button>
            <Button type="button" variant="outline" disabled={busy} onClick={() => setMode(null)}>取消编辑</Button>
          </div>
        </form>}
        <section aria-label="事件时间线" className="space-y-3">
          <h3 className="font-semibold">事件时间线</h3>
          <ol className="space-y-4">
            {detail.events.map((event) => <EventRow key={event.id} event={event} />)}
          </ol>
          <p className="text-xs text-muted-foreground">来源未提供原话会话跳转；有源事项可通过「去处理」定位。</p>
        </section>
      </div>}
      {denied && <p role="alert">无权访问账本，已停用提交。</p>}
    </aside>
  </div>;
}

function safeRef(ref: LedgerRef): string | null {
  if (ref.kind === "vault") return null;
  try {
    const url = new URL(ref.ref);
    return url.protocol === "http:" || url.protocol === "https:" ? ref.ref : null;
  } catch {
    return null;
  }
}
const ACTIONS: Record<LedgerEvent["action"], string> = { add: "新增", note: "备注", amend: "编辑", confirm: "确认", close: "完成", cancel: "撤销", reopen: "重新打开", supersede: "被新决定取代" };
const FIELDS: Record<string, string> = { title: "标题", detail: "详情", due_at: "到期", owner: "负责人", owner_kind: "负责人类型", owner_ref: "负责人", project_id: "项目" };

function displayChange(value: unknown) {
  return value == null || value === "" ? "已清除" : typeof value === "string" || typeof value === "number" ? String(value) : "已更新";
}

function EventRow({ event }: { event: LedgerEvent; }) {
  const who = event.actor_kind === "user" ? "你本人" : event.actor_kind === "relayed" ? "转述" : "系统";
  return <li className="border-l border-border pl-3 text-sm">
    <p>
      <time className="text-xs text-muted-foreground" dateTime={event.created_at}>
        {dateText(event.created_at)}
      </time>
      ·
      {ACTIONS[event.action]}
    </p>
    <p className="text-muted-foreground">
      {who}
      {event.actor_id ? ` · ${event.actor_id}` : ""}
      {event.relayed_by ? ` · 经 ${event.relayed_by} 转述` : ""}
      ·
      {event.via === "api" && event.actor_kind !== "user" ? "API 转述" : VIA[event.via]}
      {event.session_label ? ` · ${event.session_label}` : ""}
    </p>
    {event.quote && <blockquote className="mt-2 whitespace-pre-wrap border-l-2 border-border pl-2">
      {event.quote}
    </blockquote>}
    {event.note && <p className="mt-2 whitespace-pre-wrap">
      {event.note}
    </p>}
    {event.changes && <ul className="mt-2 space-y-1">
      {Object.entries(event.changes).map(([field, value]) => <li key={field}>
        {FIELDS[field] || field}
        ：
        {Array.isArray(value) && value.length === 2 ? `${displayChange(value[0])} → ${displayChange(value[1])}` : displayChange(value)}
      </li>)}
    </ul>}
  </li>;
}

function writeFailure(error: unknown) {
  if (error instanceof V1Error) {
    if (error.status === 400) return `${error.field ? `${FIELDS[error.field] || error.field}：` : "输入有误："}${error.message}`;
    if (error.status === 403) return "无权访问账本，已停用提交。";
    if (error.status === 409) return `发生冲突：${error.message}。已重新读取当前事项，输入已保留。`;
  }
  return `未确认成功：${errorText(error)}。请先刷新核实，避免重复登记或备注。`;
}

/** A successful write closes the form; conflict and transport failures keep every input. */

function ItemForm({ item, onSaved, onCancel, onConflict }: { item?: LedgerItem; onSaved: (item: LedgerItem) => void; onCancel: () => void; onConflict?: () => Promise<void>; }) {
  const [kind, setKind] = useState<LedgerKind>(item?.kind || "pending"), [title, setTitle] = useState(item?.title || ""), [detail, setDetail] = useState(item?.detail || "");
  const [due, setDue] = useState(item?.due_at || ""), [owner, setOwner] = useState(item?.owner || "user"), [project, setProject] = useState(item?.project_id || ""), [quote, setQuote] = useState("");
  const [refs, setRefs] = useState<LedgerRef[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState(""), [field, setField] = useState<string | null>(null), [denied, setDenied] = useState(false);
  const pending = useRef(false), formRef = useRef<HTMLFormElement>(null), alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    formRef.current?.querySelector<HTMLElement>("input,textarea,select")?.focus();
  }, []);
  const sourced = Boolean(item?.source);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (pending.current || denied) return;
    setError("");
    setField(null);
    if ((!sourced && !title.trim()) || !ownerValid(owner || "user")) {
      setError(!title.trim() && !sourced ? "标题不能为空" : "负责人应为 user、manager:编号、task:编号 或 chat:引用");
      return;
    }
    pending.current = true;
    setBusy(true);
    try {
      let result: LedgerItem;
      if (item) {
        const fields: LedgerAmend = {};
        if (!sourced) {
          if (title.trim() !== item.title) fields.title = title.trim();
          if (detail !== item.detail) fields.detail = detail || null;
          if ((project || null) !== item.project_id) fields.project_id = project || null;
        }
        if ((due || null) !== item.due_at) fields.due = due || null;
        if ((owner || "user") !== item.owner) fields.owner = owner || null;
        if (!Object.keys(fields).length) {
          setError("没有修改，请更新字段后保存。");
          return;
        }
        result = await ledgerAction({ item: item.id, action: "amend", fields });
      } else {
        const body: LedgerAdd = { kind, title: title.trim(), detail, due: due || null, owner: owner || "user", project_id: project || null, quote: quote || null, refs };
        result = await ledgerAdd(body);
      }
      if (alive.current) onSaved(result);
    } catch (err) {
      if (!alive.current) return;
      setError(writeFailure(err));
      if (err instanceof V1Error) {
        setField(err.field || null);
        if (err.status === 403) setDenied(true);
        if (err.status === 409) await onConflict?.();
      }
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  }
  return <form ref={formRef} className="space-y-3" onSubmit={submit}>
    <fieldset disabled={busy || denied} className="min-w-0 space-y-3">
      {!item && <label className="block text-sm">
        类型
        <select aria-label="事项类型" className={FIELD} value={kind} onChange={(e) => setKind(e.target.value as LedgerKind)}>
          {Object.entries(KINDS).map(([key, label]) => <option key={key} value={key}>
            {label}
          </option>)}
        </select>
      </label>}
      {kind === "decision" && !item && <p className="text-sm text-warning">保存为本人已确认的决定。</p>}
      {!sourced && <><label className="block text-sm">
        标题
        <Input
          aria-label="标题"
          name="title"
          aria-invalid={field?.split(".").at(-1) === "title"}
          className="pointer-coarse:text-base"
          value={title}
          maxLength={200}
          required
          onChange={(e) => setTitle(e.target.value)}
        />
      </label><label className="block text-sm">
          详情
          <textarea aria-label="详情" className={FIELD} value={detail} maxLength={4000} rows={3} onChange={(e) => setDetail(e.target.value)} />
        </label></>}
      <label className="block text-sm">
        到期（日期或 ISO 时间，留空清除）
        <Input
          aria-label="到期"
          name="due"
          aria-invalid={field?.split(".").at(-1) === "due"}
          className="pointer-coarse:text-base"
          value={due}
          maxLength={100}
          placeholder="2026-12-01"
          onChange={(e) => setDue(e.target.value)}
        />
      </label>
      <label className="block text-sm">
        负责人
        <Input
          aria-label="负责人"
          name="owner"
          aria-invalid={field?.split(".").at(-1) === "owner"}
          className="pointer-coarse:text-base"
          value={owner}
          placeholder="user / manager:编号 / task:编号 / chat:引用"
          onChange={(e) => setOwner(e.target.value)}
        />
      </label>
      {!sourced && <label className="block text-sm">
        项目 ID（留空清除）
        <Input
          aria-label="项目"
          name="project_id"
          aria-invalid={field?.split(".").at(-1) === "project_id"}
          className="pointer-coarse:text-base"
          value={project}
          onChange={(e) => setProject(e.target.value)}
        />
      </label>}
      {!item && <><label className="block text-sm">
        原话（可选，记录你当前输入）
        <textarea aria-label="原话" className={FIELD} value={quote} maxLength={1000} rows={2} onChange={(e) => setQuote(e.target.value)} />
      </label><div className="space-y-2">
          <p className="text-sm">相关引用</p>
          {refs.map((ref, n) => <div key={n} className="grid min-w-0 gap-2 rounded-md border border-border p-2">
            <select
              aria-label={`引用 ${n + 1} 类型`}
              className={FIELD}
              value={ref.kind}
              onChange={(e) => setRefs((old) => old.map((r, index) => index === n ? { ...r, kind: e.target.value as LedgerRef["kind"] } : r))}
            >
              {["task", "gate", "ledger", "intent", "pr", "doc", "vault", "url"].map((key) => <option key={key}>
                {key}
              </option>)}
            </select>
            <Input
              className="pointer-coarse:text-base"
              aria-label={`引用 ${n + 1}`}
              value={ref.ref}
              maxLength={500}
              required
              onChange={(e) => setRefs((old) => old.map((r, index) => index === n ? { ...r, ref: e.target.value } : r))}
            />
            <Input
              className="pointer-coarse:text-base"
              aria-label={`引用 ${n + 1} 标签`}
              value={ref.label || ""}
              maxLength={200}
              onChange={(e) => setRefs((old) => old.map((r, index) => index === n ? { ...r, label: e.target.value } : r))}
            />
            <Button type="button" variant="outline" onClick={() => setRefs((old) => old.filter((_, index) => index !== n))}>
              移除引用
              {n + 1}
            </Button>
          </div>)}
          {refs.length < 10 && <Button variant="outline" type="button" onClick={() => setRefs((old) => [...old, { kind: "url", ref: "" }])}>添加引用</Button>}
        </div></>}
    </fieldset>
    {error && <p role="alert" className="text-sm text-destructive">
      {error}
    </p>}
    <div className="flex flex-wrap gap-2">
      <Button type="submit" disabled={busy || denied}>
        {busy ? "保存中…" : "保存"}
      </Button>
      <Button type="button" variant="outline" disabled={busy} onClick={onCancel}>取消编辑</Button>
    </div>
  </form>;
}
