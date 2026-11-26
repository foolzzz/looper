import { Fragment, useEffect, useRef, useState } from "react";
import { Markdown } from "@/components/markdown";
import { TaskGates } from "@/components/task-gates";
import { Button } from "@/components/ui/button";
import { fetchTaskActivityWindow, isActiveTask, streamTaskActivity, type ActivityEvent, type TaskDetail, type UserGate } from "@/lib/api";
import { formatWhen } from "@/lib/work";
import { gateAnchor, isGateShown } from "@/lib/gates";
import { cn } from "@/lib/utils";
import type { WorkIdentity, WorkPaneMessage } from "./work-conversation";
import { useWorkConversationStore, workConversationKey } from "@/lib/use-work-conversation-state";

/** Presentation records only. The task-thread adapter owns decoding server events. */
export type WorkFeedRecord =
  | { source: "tool"; id: string; created_at?: string; name: string; args?: unknown; result?: unknown; isError?: boolean }
  | { source: "task-event"; id: string; created_at?: string; kind: string; text: string };

export type WorkFeedRow = { key: string; time: number } & (
  | { source: "message"; id: string; message: WorkPaneMessage }
  | { source: "activity"; id: number; activity: ActivityEvent }
  | { source: "gates"; id: string }
  | WorkFeedRecord
);
const SOURCE_ORDER = { message: 0, activity: 1, "task-event": 2, tool: 3, gates: 4 };
const timestamp = (iso?: string | null) => {
  const value = Date.parse(iso ?? "");
  return Number.isFinite(value) ? value : Infinity;
};
const bySeq = (events: ActivityEvent[]) => [...new Map(events.map(event => [event.seq, event])).values()];
const text = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value, null, 2);

/** Right-column summaries locate the existing editor; they never mount a second gate. */
export function openWorkGate(step: string, scope: ParentNode = document): boolean {
  const anchor = [...scope.querySelectorAll<HTMLElement>("[id]")].find(node => node.id === gateAnchor(step));
  if (!anchor) return false;
  if (anchor instanceof HTMLDetailsElement) anchor.open = true;
  anchor.scrollIntoView?.({ block: "nearest" });
  const target = anchor.querySelector<HTMLElement>("textarea") ?? anchor.querySelector<HTMLElement>("summary, h2") ?? anchor;
  if (target.tagName === "H2") target.tabIndex = -1;
  target.focus();
  return true;
}

export function WorkMessageRow({ message }: { message: WorkPaneMessage }) {
  const known = message.role === "user" || message.role === "assistant";
  return (
    <div data-message-id={message.id} data-role={message.role}
      className={cn("flex", message.role === "user" ? "justify-end" : "justify-start")}>
      <div className={cn("min-w-0 max-w-[85%] rounded-lg px-4 py-2.5 text-sm [overflow-wrap:anywhere]",
        message.role === "user" ? "border border-border bg-card text-card-foreground"
          : known ? "bg-muted text-foreground" : "text-muted-foreground")}>
        {message.role === "assistant" ? <Markdown>{message.content}</Markdown>
          : <p className="whitespace-pre-wrap">{message.content}</p>}
        {message.outcome === "stopped" && <p className="mt-1.5 text-xs text-muted-foreground">已停止</p>}
      </div>
    </div>
  );
}

/** Work's single time-ordered stream. It shares the page's detail and owns only activity reads. */
export function WorkSinceAway() {
  return <p role="separator" className="my-3 border-t border-border pt-2 text-center text-xs text-muted-foreground">你离开之后</p>;
}

export function WorkActivityFeed({ identity, messages, task, onGateChanged, records = [], dividerBefore }: {
  identity: WorkIdentity;
  messages: WorkPaneMessage[];
  task?: TaskDetail;
  onGateChanged?: (gate: UserGate) => void;
  records?: WorkFeedRecord[];
  dividerBefore?: string | null;
}) {
  const store = useWorkConversationStore();
  const cacheKey = workConversationKey(identity);
  const cached = store.cached(cacheKey);
  const owner = identity.kind === "planner" ? `planner:${identity.taskId}` : `manager:${identity.sessionId ?? ""}`;
  const taskId = identity.kind === "planner" ? identity.taskId : "";
  const shared = task?.task_id === taskId ? task : undefined;
  const active = !!shared && isActiveTask(shared);
  const current = useRef(owner);
  current.current = owner;
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState({ owner, events: cached?.activity ?? [] as ActivityEvent[], loading: true, earlier: false,
    hasMore: cached?.hasMore ?? false, error: "" as "" | "latest" | "earlier" | "live" });
  const loaded = useRef(state);
  loaded.current = state;
  const earlierRequest = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (state.owner === owner && !state.loading) store.cache(cacheKey, { activity: state.events, hasMore: state.hasMore });
  }, [state, owner, store, cacheKey]);

  useEffect(() => {
    if (!taskId) return;
    const ctrl = new AbortController();
    let stop: (() => void) | undefined;
    let paging = false;
    const previous = loaded.current.owner === owner ? loaded.current : undefined;
    let earliest = previous?.events.length ? Math.min(...previous.events.map(event => event.seq)) : undefined;
    const valid = () => !ctrl.signal.aborted && current.current === owner;
    setState({ owner, events: previous?.events ?? [], loading: true, earlier: false, hasMore: previous?.hasMore ?? false, error: "" });
    const readEarlier = async () => {
      if (paging || earliest === undefined || !valid()) return;
      paging = true;
      setState(value => ({ ...value, earlier: true, error: "" }));
      try {
        const window = await fetchTaskActivityWindow(taskId, earliest, ctrl.signal);
        if (!valid()) return;
        const events = window.events.filter(event => event.task_id === taskId);
        if (events.length) earliest = Math.min(earliest, ...events.map(event => event.seq));
        setState(value => ({ ...value, events: bySeq([...events, ...value.events]), hasMore: window.hasMore, earlier: false }));
      } catch {
        if (valid()) setState(value => ({ ...value, earlier: false, error: "earlier" }));
      } finally { paging = false; }
    };
    earlierRequest.current = () => { void readEarlier(); };
    void (async () => {
      try {
        const window = await fetchTaskActivityWindow(taskId, undefined, ctrl.signal);
        if (!valid()) return;
        const events = bySeq(window.events.filter(event => event.task_id === taskId));
        if (events.length) earliest = Math.min(earliest ?? Infinity, ...events.map(event => event.seq));
        setState(value => ({ owner, events: bySeq([...value.events, ...events]),
          hasMore: Math.min(...value.events.map(event => event.seq)) < Math.min(...events.map(event => event.seq))
            ? value.hasMore : window.hasMore,
          loading: false, earlier: false, error: "" }));
        if (!active) return;
        // The cursor belongs to this result, never to the render that started the read.
        const cursor = Math.max(0, ...events.map(event => event.seq));
        stop = streamTaskActivity(taskId, cursor, event => {
          if (!valid() || event.task_id !== taskId) return;
          setState(value => ({ ...value, events: bySeq([...value.events, event]) }));
        }, () => {
          if (valid()) setState(value => ({ ...value, error: "live" }));
        });
      } catch {
        if (valid()) setState(value => ({ ...value, loading: false, error: "latest" }));
      }
    })();
    return () => { ctrl.abort(); stop?.(); earlierRequest.current = null; };
  }, [owner, taskId, active, retry]);

  const view = state.owner === owner ? state : { events: [], loading: true, earlier: false, hasMore: false, error: "" };
  const rows: WorkFeedRow[] = [...new Map(messages.map(message => [message.id, message])).values()]
    .map(message => ({ source: "message", id: message.id, key: `message:${message.id}`, time: timestamp(message.created_at), message }));
  for (const activity of view.events) rows.push({ source: "activity", id: activity.seq, key: `activity:${activity.seq}`,
    time: timestamp(activity.created_at), activity });
  const merged = new Map<string, WorkFeedRecord>();
  for (const record of records) {
    const key = `${record.source}:${record.id}`;
    const previous = merged.get(key);
    merged.set(key, { ...previous, ...record, created_at: previous?.created_at ?? record.created_at } as WorkFeedRecord);
  }
  for (const [key, record] of merged) rows.push({ ...record, key, time: timestamp(record.created_at) });
  const gates = shared?.gates?.filter(isGateShown) ?? [];
  if (gates.length && onGateChanged) rows.push({ source: "gates", id: taskId, key: `gates:${taskId}`,
    time: Math.max(...gates.map(gate => timestamp(gate.opened_at))) });
  rows.sort((a, b) => (a.time === b.time ? 0 : a.time < b.time ? -1 : 1)
    || SOURCE_ORDER[a.source] - SOURCE_ORDER[b.source]
    || (typeof a.id === "number" && typeof b.id === "number" ? a.id - b.id : a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  return (
    <>
      {taskId && view.loading && <p role="status" data-work-rows-loading className="text-xs text-muted-foreground">正在加载活动…</p>}
      {taskId && !view.loading && !view.error && !view.events.length && <p role="status" className="text-xs text-muted-foreground">暂无活动</p>}
      {view.error && <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
        活动读取失败<Button variant="outline" onClick={() => view.error === "earlier" ? earlierRequest.current?.() : setRetry(n => n + 1)}>重试</Button>
      </p>}
      {view.hasMore && <Button variant="outline" disabled={view.loading || view.earlier} onClick={() => earlierRequest.current?.()}>
        {view.earlier ? "正在加载活动…" : "加载更早活动"}
      </Button>}
      {rows.map(row => {
        if (row.source === "tool") return <Fragment key={row.key}>
          {dividerBefore === row.key && <WorkSinceAway />}
          <details data-feed-key={row.key} data-row-time={Number.isFinite(row.time) ? row.time : undefined} className="shrink-0 rounded-lg border border-border text-xs">
          <summary className="min-h-11 cursor-pointer break-words px-3 py-2 focus-visible:ring-2 focus-visible:ring-ring">
            工具 · {row.name} · {row.isError ? "失败" : row.result === undefined ? "进行中" : "完成"}
          </summary>
          <div className="space-y-2 border-t border-border p-3">
            {row.args !== undefined && <pre className="whitespace-pre-wrap [overflow-wrap:anywhere]">{text(row.args)}</pre>}
            {row.result !== undefined && <pre className="whitespace-pre-wrap [overflow-wrap:anywhere]">{text(row.result)}</pre>}
          </div>
        </details></Fragment>;
        // Contain TaskGates' absolute sr-only label inside the row, even far down a long feed.
        return <div key={row.key} data-feed-key={row.key} data-row-time={Number.isFinite(row.time) ? row.time : undefined} data-activity-seq={row.source === "activity" ? row.id : undefined} className="relative min-w-0 shrink-0">
          {dividerBefore === row.key && <WorkSinceAway />}
          {row.source === "message" ? <WorkMessageRow message={row.message} />
            : row.source === "gates" ? <TaskGates task={shared!} onChanged={onGateChanged!} language="zh-CN" />
              : row.source === "task-event" ? <details className="rounded border border-border text-xs text-muted-foreground">
                  <summary className="min-h-11 cursor-pointer px-3 py-2 focus-visible:ring-2 focus-visible:ring-ring">任务事件</summary>
                  <p className="px-3 pb-2 whitespace-pre-wrap [overflow-wrap:anywhere]">{row.kind} · {row.text}</p>
                </details>
                : <details className="rounded border border-border text-xs text-muted-foreground">
                  <summary className="min-h-11 cursor-pointer px-3 py-2 [overflow-wrap:anywhere] focus-visible:ring-2 focus-visible:ring-ring">
                    {row.activity.kind === "phase" ? "进度更新" : row.activity.summary || "活动事件"}
                  </summary>
                  <p className="px-3 pb-2 whitespace-pre-wrap [overflow-wrap:anywhere]">
                    {row.activity.kind} · {row.activity.worker || "系统"} · {formatWhen(row.activity.created_at)}
                    {row.activity.kind === "phase" && `\n${row.activity.summary}`}
                    {row.activity.detail && `\n${row.activity.detail}`}
                  </p>
                </details>}
        </div>;
      })}
    </>
  );
}
