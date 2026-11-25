import { SessionEndedError } from "@/lib/session";
import { callV1, V1Error } from "@/lib/v1";
import { isMissingOperation } from "@/lib/work";

/**
 * Write body and result match the closed operation
 * tasks.task_manager_profile_set: only task_id and profile_id.
 * The effective model, runtime, provider and source are not in that result.
 * POST /api/v1/tasks/task_manager_profile_get is the panel's read for them.
 * It is not a registered operation; an unknown route is remembered and not
 * requested again.
 */
export const TASK_MANAGER_PROFILE_SET_PATH = "/api/v1/tasks/task_manager_profile_set";
export const TASK_MANAGER_PROFILE_READ_PATH = "/api/v1/tasks/task_manager_profile_get";

export interface TaskManagerChoice {
  profile_id: string;
  name: string;
  model: string;
  runtime: string;
  provider: string;
  source: string;
  problem?: string | null;
  compatible?: boolean;
}

export interface TaskManagerEffective {
  profile_id: string;
  name: string;
  model: string;
  runtime: string;
  provider: string;
  source: string;
}

export interface TaskManagerResume {
  session_id?: string | null;
  resumed?: boolean;
  session_fresh?: { reason: string } | null;
  history?: unknown;
  cost_usd?: number | null;
  background_roles?: {
    planner?: string | null;
    worker?: string | null;
    reviewer?: string | null;
  };
}

export interface TaskManagerProfileView {
  choices: TaskManagerChoice[];
  effective: TaskManagerEffective | null;
  inherit: boolean;
  selected_profile_id: string | null;
  resume?: TaskManagerResume;
}

export type ProfileReadResult =
  | { ok: true; view: TaskManagerProfileView }
  | { ok: false; reason: "missing" }
  | { ok: false; reason: "error" };

export type ProfileSetResult =
  | { ok: true; profile_id: string | null }
  | { ok: false; reason: "missing" }
  | { ok: false; reason: "busy" }
  | { ok: false; reason: "runtime"; message: string; requested: string | null; supported: string | null }
  | { ok: false; reason: "invalid"; message: string }
  | { ok: false; reason: "failure"; message: string }
  | { ok: false; reason: "uncertain" };

let routeMissing = false;

export function resetTaskManagerProfileProbe(): void {
  routeMissing = false;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isAbort(err: unknown): boolean {
  return typeof err === "object" && err !== null && "name" in err && (err as { name: string }).name === "AbortError";
}

function isPending(value: unknown): boolean {
  return isRecord(value) && value.status === "needs_confirmation";
}

function detailString(err: V1Error, key: string): string | null {
  const details = err.details as Record<string, unknown> | undefined;
  const value = details?.[key];
  return typeof value === "string" && value ? value : null;
}

/** A missing task is not an unknown route. Any other 404 is, including "no post …". */
function isUnknownRoute(err: unknown): boolean {
  if (isMissingOperation(err)) return true;
  if (!(err instanceof V1Error) || err.status !== 404) return false;
  return !(err.code === "not_found" && err.message.toLowerCase().includes("task not found"));
}

function markMissing(err: unknown): boolean {
  if (!isUnknownRoute(err)) return false;
  routeMissing = true;
  return true;
}

function parseProfile(value: unknown): TaskManagerEffective | null {
  if (!isRecord(value)) return null;
  if (!text(value.profile_id) || !text(value.name) || !text(value.model)) return null;
  if (!text(value.runtime) || !text(value.provider) || !text(value.source)) return null;
  return {
    profile_id: value.profile_id,
    name: value.name,
    model: value.model,
    runtime: value.runtime,
    provider: value.provider,
    source: value.source,
  };
}

function parseResume(value: unknown): TaskManagerResume | undefined | null {
  if (value === undefined) return undefined;
  if (!isRecord(value)) return null;
  const resume: TaskManagerResume = {};
  if (value.session_id !== undefined) {
    if (value.session_id !== null && typeof value.session_id !== "string") return null;
    resume.session_id = value.session_id;
  }
  if (value.resumed !== undefined) {
    if (typeof value.resumed !== "boolean") return null;
    resume.resumed = value.resumed;
  }
  if (value.session_fresh !== undefined && value.session_fresh !== null) {
    if (!isRecord(value.session_fresh) || typeof value.session_fresh.reason !== "string") return null;
    resume.session_fresh = { reason: value.session_fresh.reason };
  } else if (value.session_fresh === null) {
    resume.session_fresh = null;
  }
  if ("history" in value) resume.history = value.history;
  if (value.cost_usd !== undefined) {
    if (value.cost_usd !== null && typeof value.cost_usd !== "number") return null;
    resume.cost_usd = value.cost_usd;
  }
  if (value.background_roles !== undefined) {
    if (!isRecord(value.background_roles)) return null;
    const roles: NonNullable<TaskManagerResume["background_roles"]> = {};
    for (const key of ["planner", "worker", "reviewer"] as const) {
      const role = value.background_roles[key];
      if (role !== undefined && role !== null && typeof role !== "string") return null;
      if (role !== undefined) roles[key] = role;
    }
    resume.background_roles = roles;
  }
  return resume;
}

function parseView(value: unknown): TaskManagerProfileView | null {
  if (!isRecord(value) || !Array.isArray(value.choices) || typeof value.inherit !== "boolean") return null;
  if (!(value.selected_profile_id === null || text(value.selected_profile_id))) return null;
  let effective: TaskManagerEffective | null = null;
  if (value.effective !== null) {
    effective = parseProfile(value.effective);
    if (!effective) return null;
  }
  const choices: TaskManagerChoice[] = [];
  for (const item of value.choices) {
    const profile = parseProfile(item);
    if (!profile || !isRecord(item)) return null;
    if (item.problem !== undefined && item.problem !== null && !text(item.problem)) return null;
    if (item.compatible !== undefined && typeof item.compatible !== "boolean") return null;
    choices.push({
      ...profile,
      ...(item.problem !== undefined ? { problem: item.problem } : {}),
      ...(item.compatible !== undefined ? { compatible: item.compatible } : {}),
    });
  }
  const resume = parseResume(value.resume);
  if (value.resume !== undefined && resume === null) return null;
  return {
    choices,
    effective,
    inherit: value.inherit,
    selected_profile_id: value.selected_profile_id,
    ...(resume ? { resume } : {}),
  };
}

/** Closed result: exactly task_id and profile_id. Anything else is not proof of a switch. */
function parseSetResult(value: unknown): { task_id: string; profile_id: string | null } | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes("task_id") || !keys.includes("profile_id")) return null;
  if (!text(value.task_id)) return null;
  if (value.profile_id !== null && !text(value.profile_id)) return null;
  return { task_id: value.task_id, profile_id: value.profile_id };
}

function isRuntime(err: V1Error): boolean {
  return err.code === "task_manager_runtime_unsupported"
    || err.message.includes("task_manager_runtime_unsupported")
    || err.message.includes("runs only on Claude Code");
}

function isBusy(err: V1Error): boolean {
  if (err.message.toLowerCase().includes("work mode is off")) return false;
  if (err.code === "task_manager_busy") return true;
  if (err.status !== 409) return false;
  return err.message.includes("stop its turn") || err.message.includes("working in this task");
}

function classify(err: V1Error): ProfileSetResult {
  if (isRuntime(err)) {
    return {
      ok: false,
      reason: "runtime",
      message: err.message,
      requested: detailString(err, "requested"),
      supported: detailString(err, "supported"),
    };
  }
  if (isBusy(err)) return { ok: false, reason: "busy" };
  if (err.code === "invalid" || err.status === 400) return { ok: false, reason: "invalid", message: err.message };
  return { ok: false, reason: "failure", message: err.message };
}

export async function readTaskManagerProfile(taskId: string | null, signal?: AbortSignal): Promise<ProfileReadResult> {
  if (routeMissing) return { ok: false, reason: "missing" };
  try {
    const result = await callV1<unknown>(
      TASK_MANAGER_PROFILE_READ_PATH,
      taskId === null ? {} : { task_id: taskId },
      { signal },
    );
    if (isPending(result)) return { ok: false, reason: "error" };
    const view = parseView(result);
    return view ? { ok: true, view } : { ok: false, reason: "error" };
  } catch (err) {
    if (signal?.aborted || isAbort(err)) throw err;
    if (markMissing(err)) return { ok: false, reason: "missing" };
    return { ok: false, reason: "error" };
  }
}

export async function setTaskManagerProfile(
  taskId: string,
  profileId: string | null,
  signal?: AbortSignal,
): Promise<ProfileSetResult> {
  if (routeMissing) return { ok: false, reason: "missing" };
  try {
    const result = await callV1<unknown>(
      TASK_MANAGER_PROFILE_SET_PATH,
      { task_id: taskId, profile_id: profileId },
      { signal },
    );
    if (isPending(result)) return { ok: false, reason: "uncertain" };
    const parsed = parseSetResult(result);
    if (!parsed || parsed.task_id !== taskId || parsed.profile_id !== profileId) return { ok: false, reason: "uncertain" };
    return { ok: true, profile_id: parsed.profile_id };
  } catch (err) {
    if (signal?.aborted || isAbort(err)) throw err;
    if (markMissing(err)) return { ok: false, reason: "missing" };
    if (err instanceof SessionEndedError) return { ok: false, reason: "failure", message: err.message };
    if (err instanceof V1Error) return classify(err);
    return { ok: false, reason: "uncertain" };
  }
}
