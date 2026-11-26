import { createContext, createElement, useContext, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import type { ActivityEvent, TaskDetail } from "@/lib/api";
import type { WorkIdentity, WorkPaneMessage } from "@/components/work-conversation";

type Anchor = { key: string; offset: number };
type Watermark = { key: string; seq: number; time: number | null };
type View = { anchor: Anchor | null; top: number; bottom: boolean };
interface ConversationState {
  version: 1;
  draft: string;
  staged: string[];
  history: string[];
  view: View;
  left: Watermark | null;
}
interface CachedConversation {
  messages: WorkPaneMessage[];
  messagesLoaded: boolean;
  activity: ActivityEvent[];
  hasMore: boolean;
  task?: TaskDetail;
}
const initial = (): ConversationState => ({ version: 1, draft: "", staged: [], history: [],
  view: { anchor: null, top: 0, bottom: true }, left: null });
export function workConversationKey(identity: WorkIdentity): string {
  const owner = identity.kind === "planner" ? identity.taskId : identity.managerId ?? "manager";
  const sid = identity.sessionId ?? (identity.kind === "planner" ? `planner-${owner}` : "");
  return `looper.work.conversation.v1.${encodeURIComponent(JSON.stringify([identity.kind, owner, sid]))}`;
}
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === "string");
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
function valid(value: ConversationState): boolean {
  return !!value && value.version === 1 && typeof value.draft === "string" && strings(value.staged) && strings(value.history)
    && !!value.view && finite(value.view.top) && typeof value.view.bottom === "boolean"
    && (value.view.anchor === null || (!!value.view.anchor && typeof value.view.anchor.key === "string" && finite(value.view.anchor.offset)))
    && (value.left === null || (!!value.left && typeof value.left.key === "string" && finite(value.left.seq)
      && (value.left.time === null || finite(value.left.time))));
}

/** View/composer state survives cache eviction. Sensitive conversation rows never enter storage. */
export function createWorkConversationStore() {
  const states = new Map<string, ConversationState>();
  const caches = new Map<string, CachedConversation>();
  const listeners = new Set<() => void>();
  let selected = "";
  const read = (key: string) => {
    if (!states.has(key)) {
      let value = initial();
      try {
        const saved = JSON.parse(sessionStorage.getItem(key) ?? "null") as ConversationState;
        if (valid(saved)) value = saved;
      } catch { /* Blocked storage, old and corrupt values fall back to memory. */ }
      states.set(key, value);
    }
    return states.get(key)!;
  };
  const patch = (key: string, update: Partial<ConversationState>) => {
    const value = { ...read(key), ...update };
    states.set(key, value);
    // Input history comes from server messages again on reload, like the conversation itself.
    try { sessionStorage.setItem(key, JSON.stringify({ ...value, history: [] })); } catch { /* Memory stays writable. */ }
    listeners.forEach(listener => listener());
  };
  const cached = (key: string) => {
    const value = caches.get(key);
    if (value) { caches.delete(key); caches.set(key, value); }
    return value;
  };
  return {
    read, patch, cached,
    patchView: (key: string, view: View) => patch(key, { view }),
    subscribe: (callback: () => void) => { listeners.add(callback); return () => { listeners.delete(callback); }; },
    select: (key: string) => { selected = key; },
    cache: (key: string, update: Partial<CachedConversation>) => {
      const value = { messages: [], messagesLoaded: false, activity: [], hasMore: false, ...caches.get(key), ...update };
      if (update.messages !== undefined) value.messagesLoaded = true;
      // Keep a contiguous window around an older reading anchor, otherwise the latest rows.
      const rows = [...value.messages.map(row => ({ key: `message:${row.id}`, time: row.created_at, row, source: "message" as const })),
        ...value.activity.map(row => ({ key: `activity:${row.seq}`, time: row.created_at, row, source: "activity" as const }))]
        .sort((a, b) => (Date.parse(a.time ?? "") || 0) - (Date.parse(b.time ?? "") || 0));
      const anchorIndex = rows.findIndex(row => row.key === read(key).view.anchor?.key);
      const start = anchorIndex >= 0 && anchorIndex < rows.length - 1000
        ? Math.max(0, anchorIndex - 500) : Math.max(0, rows.length - 1000);
      const window = rows.slice(start, start + 1000);
      if (start > 0 && value.activity.length) value.hasMore = true;
      value.messages = window.flatMap(item => item.source === "message" ? [{ ...item.row, streaming: false }] : []);
      value.activity = window.flatMap(item => item.source === "activity" ? [item.row] : []);
      caches.delete(key); caches.set(key, value);
      while (caches.size > 20) {
        const oldest = [...caches.keys()].find(candidate => candidate !== selected);
        if (!oldest) break;
        caches.delete(oldest);
      }
    },
    clear: () => { states.clear(); caches.clear(); selected = ""; },
  };
}
type Store = ReturnType<typeof createWorkConversationStore>;
// Standalone panes use this fallback; the Work page owns a fresh store for its lifetime.
const standalone = createWorkConversationStore();
const Context = createContext<Store>(standalone);
export const clearWorkConversationMemory = () => standalone.clear();
export function WorkConversationStateProvider({ children, store }: { children: ReactNode; store?: Store }) {
  const [owned] = useState(createWorkConversationStore);
  return createElement(Context.Provider, { value: store ?? owned }, children);
}
export const useWorkConversationStore = () => useContext(Context);

export function useWorkConversationState(identity: WorkIdentity) {
  const store = useWorkConversationStore();
  const key = workConversationKey(identity);
  const value = useSyncExternalStore(store.subscribe, () => store.read(key));
  return { key, store, value,
    setDraft: (draft: string) => store.patch(key, { draft }),
    setStaged: (staged: string[]) => store.patch(key, { staged: [...staged] }),
    rememberHistory: (history: string[]) => {
      const next = history.slice(-100);
      if (JSON.stringify(next) !== JSON.stringify(store.read(key).history)) store.patch(key, { history: next });
    },
  };
}

/** Observe row geometry as well as React renders: tools, prepends and hidden panes change layout later. */
export function useWorkConversationViewport(identity: WorkIdentity, ready: boolean) {
  const store = useWorkConversationStore();
  const key = workConversationKey(identity);
  const viewport = useRef<HTMLDivElement>(null);
  const [divider, setDivider] = useState<string | null>(null);
  const adjust = useRef<(() => void) | null>(null);
  const send = useRef<{ key: string; run: () => void } | null>(null);
  const isReady = useRef(ready);
  isReady.current = ready;
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    store.select(key);
    const saved = store.read(key);
    const left = saved.left;
    let view = saved.view;
    let displayed: Watermark | null = null;
    let restored = false;
    let forceBottom = false;
    // Returning readers keep their departure position through cache revalidation,
    // even if that position used to be the bottom. User scrolling or sending resumes following.
    let followBottom = saved.view.anchor === null;
    let positionedTop: number | null = null;
    let alive = true;
    const observed = new Set<HTMLElement>();
    const rows = () => [...element.querySelectorAll<HTMLElement>("[data-feed-key]")];
    const visible = () => element.clientHeight > 0;
    const position = (top: number) => { element.scrollTop = top; positionedTop = element.scrollTop; };
    const capture = () => {
      if (!alive || !visible() || !isReady.current || !restored) return;
      const top = element.getBoundingClientRect().top;
      const anchor = rows().find(row => row.getBoundingClientRect().bottom > top);
      view = { top: element.scrollTop, bottom: element.scrollHeight - element.scrollTop - element.clientHeight < 24,
        anchor: anchor ? { key: anchor.dataset.feedKey!, offset: anchor.getBoundingClientRect().top - top } : null };
    };
    const update = () => {
      if (!alive || !visible() || !isReady.current) return;
      const all = rows();
      // A task header can arrive before its history, and activity has its own read.
      // Do not replace a saved anchor with the geometry of that placeholder.
      if (!restored && view.anchor && !all.some(row => row.dataset.feedKey === view.anchor!.key)
        && element.querySelector("[data-work-rows-loading]")) return;
      if (forceBottom || (restored && view.bottom && followBottom)) position(element.scrollHeight);
      else {
        const anchor = all.find(row => row.dataset.feedKey === view.anchor?.key);
        if (anchor && view.anchor) position(element.scrollTop + anchor.getBoundingClientRect().top - element.getBoundingClientRect().top - view.anchor.offset);
        else if (!restored) position(view.anchor || !view.bottom ? view.top : element.scrollHeight);
      }
      restored = true; forceBottom = false;
      const last = all.at(-1);
      if (last) {
        const seq = Math.max(0, ...all.map(row => Number(row.dataset.activitySeq) || 0));
        const time = Number(last.dataset.rowTime);
        displayed = { key: last.dataset.feedKey!, seq, time: Number.isFinite(time) && last.dataset.rowTime ? time : null };
      }
      if (left) {
        const index = all.findIndex(row => row.dataset.feedKey === left.key);
        const next = index >= 0 ? all[index + 1] : all.find(row =>
          Number(row.dataset.activitySeq) > left.seq || (left.time !== null && Number(row.dataset.rowTime) > left.time));
        setDivider(previous => previous === (next?.dataset.feedKey ?? null) ? previous : next?.dataset.feedKey ?? null);
      } else setDivider(previous => previous === null ? previous : null);
      capture();
      for (const row of observed) if (!element.contains(row)) { resize?.unobserve(row); observed.delete(row); }
      for (const row of all) if (!observed.has(row)) { resize?.observe(row); observed.add(row); }
    };
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    resize?.observe(element);
    const mutation = new MutationObserver(update);
    mutation.observe(element, { childList: true, subtree: true, characterData: true });
    const snapshot = () => {
      if (!visible() || !isReady.current || !restored) return;
      capture(); store.patch(key, { view, ...(displayed ? { left: displayed } : {}) });
    };
    const pagehide = () => snapshot();
    const scrolled = () => {
      if (!visible() || !isReady.current || !restored) return;
      if (element.scrollTop !== positionedTop) followBottom = true;
      capture(); store.patchView(key, view);
    };
    element.addEventListener("scroll", scrolled);
    window.addEventListener("pagehide", pagehide);
    adjust.current = update;
    send.current = { key, run: () => { forceBottom = true; followBottom = true; update(); } };
    update();
    return () => {
      alive = false;
      // Use the last displayed snapshot; the next selection may already own the DOM.
      if (restored) store.patch(key, { view, ...(displayed ? { left: displayed } : {}) });
      resize?.disconnect(); mutation.disconnect(); element.removeEventListener("scroll", scrolled);
      window.removeEventListener("pagehide", pagehide); adjust.current = null; send.current = null;
    };
  }, [key, store]);
  useLayoutEffect(() => { adjust.current?.(); });
  return { viewport, divider, afterSend: () => { if (send.current?.key === key) send.current.run(); } };
}
