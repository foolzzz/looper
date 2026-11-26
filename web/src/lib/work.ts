import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "@/lib/use-action";
import { callV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

/** Shown when `tasks.create_draft` is missing, or the conversations switch is off. */
export const DRAFTS_UNAVAILABLE = "此服务器尚不支持草稿任务";

export const FIND_UNAVAILABLE = "此服务器尚不支持查找任务。";

export interface WorkTask {
  task_id: string;
  title: string;
  created_at?: string;
  project_id: string | null;
  project_name: string | null;
  status: string;
  group: string;
  last_activity_at: string | null;
  last_seq: number | null;
  waiting_gates: number;
  needs_you: boolean;
  unseen: boolean;
}

export interface WorkSchedule {
  schedule_id: string;
  name: string;
  project_id: string | null;
  project_name: string | null;
  enabled: boolean;
  consecutive_failures: number;
  last_success_at: string | null;
  last_task_id: string | null;
  last_status: string | null;
  last_ended_at: string | null;
  running: boolean;
}

export interface WorkListResult {
  items?: WorkTask[];
  schedules?: WorkSchedule[];
  next_cursor: string | null;
  counts?: Record<string, number>;
}

export interface SelectCandidate {
  task_id: string;
  title: string | null;
  project_name: string | null;
  status: string;
  group: string;
  last_activity_at: string | null;
}

export interface SelectResult {
  kind: "one" | "candidates" | "none";
  task?: SelectCandidate;
  candidates?: SelectCandidate[];
  more?: number;
  searched?: Record<string, unknown>;
  hint?: string;
}

const LAST_PROJECT = "looper.work.last-project";
const SCROLL_KEY = "looper.work.scroll";
const SELECTION_KEY = "looper.work.selection";
const COLLAPSED_KEY = "looper.work.collapsed-projects";
const ENDED_OPEN_KEY = "looper.work.ended-open";

function readStore(store: Storage, key: string): string | null {
  try {
    return store.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(store: Storage, key: string, value: string | null) {
  try {
    if (value === null) store.removeItem(key);
    else store.setItem(key, value);
  } catch {
    // Private mode and disabled storage still leave the page usable.
  }
}

// Remember view shape, not task data: reload re-reads each cursor from the server.
function viewValue<T>(key: string, fallback: T): T {
  try { return JSON.parse(readStore(sessionStorage, `looper.work.${key}`) ?? "null") ?? fallback; }
  catch { return fallback; }
}

export const workStorage = {
  pages: (key: string): number => Math.max(1, Number(viewValue(`pages.${key}`, 1)) || 1),
  setPages: (key: string, count: number) => writeStore(sessionStorage, `looper.work.pages.${key}`, String(count)),
  expanded: (): string[] => {
    const value = viewValue<unknown>("expanded", []);
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
  },
  setExpanded: (ids: string[]) => writeStore(sessionStorage, "looper.work.expanded", JSON.stringify(ids)),
  filters: (): { project: string; status: string } => {
    const value = viewValue("filters", { project: "", status: "" });
    return { project: typeof value.project === "string" ? value.project : "", status: typeof value.status === "string" ? value.status : "" };
  },
  setFilters: (project: string, status: string) => writeStore(sessionStorage, "looper.work.filters", JSON.stringify({ project, status })),
  lastProject: () => readStore(localStorage, LAST_PROJECT),
  setLastProject: (id: string) => writeStore(localStorage, LAST_PROJECT, id),
  scroll: () => Number(readStore(sessionStorage, SCROLL_KEY) ?? "0") || 0,
  setScroll: (n: number) => writeStore(sessionStorage, SCROLL_KEY, String(n)),
  selection: () => readStore(sessionStorage, SELECTION_KEY),
  setSelection: (value: string) => writeStore(sessionStorage, SELECTION_KEY, value),
  collapsed: (): string[] => {
    try {
      const value = JSON.parse(readStore(localStorage, COLLAPSED_KEY) ?? "[]") as unknown;
      return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
    } catch {
      return [];
    }
  },
  setCollapsed: (ids: string[]) => writeStore(localStorage, COLLAPSED_KEY, JSON.stringify(ids)),
  endedOpen: () => readStore(localStorage, ENDED_OPEN_KEY) === "true",
  setEndedOpen: (open: boolean) => writeStore(localStorage, ENDED_OPEN_KEY, open ? "true" : "false"),
};

function done<T>(value: T | V1PendingConfirmation): T {
  if (value && typeof value === "object" && "status" in value && value.status === "needs_confirmation") {
    throw new V1Error(202, "unavailable", "此操作需要确认。");
  }
  return value as T;
}

/** An unknown route or tool id. A missing task (`not_found` for one id) is not this. */
export function isMissingOperation(err: unknown): boolean {
  if (!(err instanceof V1Error)) return false;
  const message = err.message.toLowerCase();
  return message.startsWith("no post ") || message.startsWith("no get ") || message.includes("no operation");
}

/**
 * Whether a POST operation is registered. GET is enough: 405 `use POST` means it exists,
 * 404 `no GET …` means it does not. Never POST — `create_draft` would insert a row.
 */
export async function operationExists(path: string, signal?: AbortSignal): Promise<boolean> {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    signal,
    headers: { Accept: "application/json" },
  });
  if (response.status !== 404) return true;
  const body = await response.json().catch(() => null) as { error?: { message?: string } } | null;
  const message = String(body?.error?.message ?? "").toLowerCase();
  return !(message.startsWith("no get ") || message.includes("no operation"));
}

export async function taskConversationsGet(signal?: AbortSignal): Promise<{ enabled: boolean }> {
  return done(await callV1<{ enabled: boolean }>("/api/v1/task_conversations/get", {}, { signal }));
}

export async function workList(
  body: {
    section: "open" | "ended" | "schedules";
    project_id?: string;
    statuses?: string[];
    cursor?: string | null;
    limit?: number;
  },
  signal?: AbortSignal,
): Promise<WorkListResult> {
  return done(await callV1<WorkListResult>("/api/v1/tasks/work_list", {
    section: body.section,
    ...(body.project_id ? { project_id: body.project_id } : {}),
    ...(body.statuses?.length ? { statuses: body.statuses } : {}),
    ...(body.cursor ? { cursor: body.cursor } : {}),
    ...(body.limit ? { limit: body.limit } : {}),
  }, { signal }));
}

export async function markSeen(taskId: string, seq: number): Promise<void> {
  done(await callV1<unknown>("/api/v1/tasks/mark_seen", { task_id: taskId, seq }));
}

export async function tasksSelect(
  body: { selection?: string; cwd?: string; all?: boolean },
  signal?: AbortSignal,
): Promise<SelectResult> {
  return done(await callV1<SelectResult>("/api/v1/tasks/select", body, { signal }));
}

export async function createDraft(projectId: string): Promise<{ task_id: string; project_id: string }> {
  return done(await callV1<{ task_id: string; project_id: string }>("/api/v1/tasks/create_draft", { project_id: projectId }));
}

const WORK_STATUS: Record<string, string> = {
  draft: "草稿", init: "待开始", running: "运行中", assigned: "已分配", planning: "规划中",
  executing: "执行中", verifying: "验证中", stopping: "停止中", waiting: "等待中", pending: "排队中",
  waiting_user: "等你回复", awaiting_budget: "等你决定花费", completed: "已完成", failed: "失败",
  stopped: "已停止", cancelled: "已取消", queued: "排队中", gating: "检查中", succeeded: "成功",
  interrupted: "已中断", timed_out: "已超时", skipped_overlap: "已跳过：任务重叠",
  skipped_missed: "已跳过：错过时间", skipped_gate: "已跳过：无新内容", gate_failed: "检查失败",
  skipped_budget: "已跳过：每日预算", skipped_busy: "已跳过：管家繁忙",
};

export function workStatusLabel(status: string): string {
  return WORK_STATUS[status] ?? status;
}

export function formatWhen(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** `#work` is null so the page can restore the stored selection. Any other sub is manager or one task. */
export function workSelection(sub: string): { kind: "manager" } | { kind: "task"; taskId: string } | null {
  if (sub === "") return null;
  if (sub === "manager") return { kind: "manager" };
  const match = /^task\/([^/?#]+)$/.exec(sub);
  return match ? { kind: "task", taskId: decodeURIComponent(match[1]) } : { kind: "manager" };
}

export function groupByProject(tasks: WorkTask[]): { id: string; name: string; tasks: WorkTask[] }[] {
  const groups: { id: string; name: string; tasks: WorkTask[] }[] = [];
  for (const task of tasks) {
    const id = task.project_id ?? "";
    let group = groups.find((item) => item.id === id);
    if (!group) {
      group = { id, name: task.project_name || "无项目", tasks: [] };
      groups.push(group);
    }
    group.tasks.push(task);
  }
  return groups;
}

export function taskTitle(title: string | null | undefined, createdAt?: string, taskId?: string): string {
  if (title?.trim() && title !== taskId) return title;
  const when = formatWhen(createdAt ?? null);
  return when ? `未命名任务 · ${when}` : "未命名任务";
}

export function unavailableCopy(section: "open" | "ended" | "schedules"): string {
  const name = section === "open" ? "进行中任务" : section === "schedules" ? "定时任务" : "已结束任务";
  return `此服务器尚不支持${name}。`;
}

export interface WorkSection<T> {
  items: T[];
  next: string | null;
  error: string | null;
  missing: boolean;
  loading: boolean;
  ready: boolean;
  retry: () => void;
  loadMore: () => void;
}

export function useWorkSection<T>(
  section: "open" | "ended" | "schedules",
  active: boolean,
  projectId = "",
  status = "",
): WorkSection<T> {
  const [items, setItems] = useState<T[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [loading, setLoading] = useState(active);
  const [ready, setReady] = useState(false);
  const pageCount = useRef(0);
  const storageKey = `${section}:${projectId}:${status}`;
  const [generation, setGeneration] = useState(0);
  const nextRef = useRef<string | null>(null);
  const seq = useRef(0);
  const loadingRef = useRef(false);

  useEffect(() => {
    if (!active) return;
    const ctrl = new AbortController();
    const token = ++seq.current;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    setMissing(false);
    setReady(false);
    const request = async () => {
      const rows: T[] = [];
      let cursor: string | null = null;
      const wanted = workStorage.pages(storageKey);
      pageCount.current = 0;
      do {
        const page = await workList({ section,
          ...(section === "ended" ? {} : { limit: 100 }),
          ...(projectId ? { project_id: projectId } : {}),
          ...(status ? { statuses: [status] } : {}),
          ...(cursor ? { cursor } : {}),
        }, ctrl.signal);
        if (token !== seq.current) return;
        rows.push(...((section === "schedules" ? page.schedules : page.items) ?? []) as T[]);
        cursor = page.next_cursor;
        pageCount.current++;
      } while (cursor && pageCount.current < wanted);
      setItems(rows);
      nextRef.current = cursor;
      setNext(cursor);
      workStorage.setPages(storageKey, pageCount.current);
    };
    request().catch((err: unknown) => {
      if (token !== seq.current || ctrl.signal.aborted) return;
      if (isMissingOperation(err)) { setMissing(true); setItems([]); }
      else setError(errorText(err));
    }).finally(() => {
      if (token !== seq.current || ctrl.signal.aborted) return;
      setLoading(false);
      loadingRef.current = false;
      setReady(true);
    });
    return () => {
      ctrl.abort();
      seq.current += 1;
      loadingRef.current = false;
    };
  }, [active, section, projectId, status, generation, storageKey]);

  const loadMore = useCallback(() => {
    if (!nextRef.current || loadingRef.current) return;
    const ctrl = new AbortController();
    const token = ++seq.current;
    const cursor = nextRef.current;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    workList({
      section,
      cursor,
      ...(section === "ended" ? {} : { limit: 100 }),
      ...(section === "ended" && projectId ? { project_id: projectId } : {}),
      ...(section === "ended" && status ? { statuses: [status] } : {}),
    }, ctrl.signal).then((page) => {
      if (token !== seq.current) return;
      const rows = (section === "schedules" ? page.schedules : page.items) ?? [];
      setItems((prev) => [...prev, ...(rows as T[])]);
      workStorage.setPages(storageKey, ++pageCount.current);
      nextRef.current = page.next_cursor;
      setNext(page.next_cursor);
      setLoading(false);
      loadingRef.current = false;
    }).catch((err: unknown) => {
      if (token !== seq.current || ctrl.signal.aborted) return;
      setLoading(false);
      loadingRef.current = false;
      if (!isMissingOperation(err)) setError(errorText(err));
    });
  }, [section, projectId, status, storageKey]);

  const retry = useCallback(() => setGeneration((n) => n + 1), []);
  return { items, next, error, missing, loading, ready, retry, loadMore };
}
