import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "@/lib/use-action";
import { callV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

/** Shown when `tasks.create_draft` is missing, or the conversations switch is off. */
export const DRAFTS_UNAVAILABLE = "Drafts are not available on this server yet";

export const FIND_UNAVAILABLE = "Finding a task is not available on this server yet.";

export interface WorkTask {
  task_id: string;
  title: string;
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
  title: string;
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

export const workStorage = {
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
    throw new V1Error(202, "unavailable", "This action needs confirmation.");
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

export function formatWhen(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
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
      group = { id, name: task.project_name || "No project", tasks: [] };
      groups.push(group);
    }
    group.tasks.push(task);
  }
  return groups;
}

export function taskTitle(title: string | null | undefined): string {
  return title?.trim() ? title : "Untitled task";
}

export function unavailableCopy(section: "open" | "ended" | "schedules"): string {
  const name = section === "open" ? "Open tasks" : section === "schedules" ? "Schedules" : "Ended tasks";
  return `${name} are not available on this server yet.`;
}

export interface WorkSection<T> {
  items: T[];
  next: string | null;
  error: string | null;
  missing: boolean;
  loading: boolean;
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
  const [loading, setLoading] = useState(false);
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
    workList({
      section,
      ...(section === "ended" ? {} : { limit: 100 }),
      ...(section === "ended" && projectId ? { project_id: projectId } : {}),
      ...(section === "ended" && status ? { statuses: [status] } : {}),
    }, ctrl.signal).then((page) => {
      if (token !== seq.current) return;
      const rows = (section === "schedules" ? page.schedules : page.items) ?? [];
      setItems(rows as T[]);
      nextRef.current = page.next_cursor;
      setNext(page.next_cursor);
      setLoading(false);
      loadingRef.current = false;
    }).catch((err: unknown) => {
      if (token !== seq.current || ctrl.signal.aborted) return;
      setLoading(false);
      loadingRef.current = false;
      if (isMissingOperation(err)) {
        setMissing(true);
        setItems([]);
        return;
      }
      setError(errorText(err));
    });
    return () => {
      ctrl.abort();
      seq.current += 1;
      loadingRef.current = false;
    };
  }, [active, section, projectId, status, generation]);

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
  }, [section, projectId, status]);

  const retry = useCallback(() => setGeneration((n) => n + 1), []);
  return { items, next, error, missing, loading, retry, loadMore };
}
