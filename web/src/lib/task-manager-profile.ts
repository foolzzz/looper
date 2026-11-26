import { SessionEndedError } from "@/lib/session";
import { callV1, V1Error } from "@/lib/v1";
import { isMissingOperation } from "@/lib/work";

/**
 * tasks.task_manager_profile_set sends {task_id, profile_id}.
 * A successful result is that pair, or the closed profile view below.
 * Anything else is not proof the switch landed.
 * Busy is HTTP 409 task_manager_busy, or conflict while a turn is active.
 * Runtime refusal is HTTP 400 task_manager_runtime_unsupported.
 * An unknown profile is HTTP 400 invalid. The message stays the server's text.
 */
export const TASK_MANAGER_PROFILE_SET_PATH = "/api/v1/tasks/task_manager_profile_set";
export const TASK_MANAGER_PROFILE_GET_PATH = "/api/v1/tasks/task_manager_profile_get";

export interface TaskManagerProfileChoice {
  profile_id: string;
  label: string;
  model: string;
  runtime: string;
}

export interface TaskManagerProfileView {
  task_id: string;
  override_profile_id: string | null;
  effective: { profile_id: string; model: string; runtime: string; provider: string; effort: string | null };
  source: string;
  choices: TaskManagerProfileChoice[];
}

export type ProfileSetResult =
  | { ok: true; profile_id: string | null }
  | { ok: false; reason: "missing" }
  | { ok: false; reason: "busy"; message: string }
  | { ok: false; reason: "invalid"; message: string }
  | { ok: false; reason: "failure"; message: string }
  | { ok: false; reason: "uncertain" };

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

/** Anything other than the two closed fields is not proof the switch landed. */
function parseSetResult(value: unknown): { task_id: string; profile_id: string | null } | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes("task_id") || !keys.includes("profile_id")) return null;
  if (!text(value.task_id)) return null;
  if (value.profile_id !== null && !text(value.profile_id)) return null;
  return { task_id: value.task_id, profile_id: value.profile_id };
}

const VIEW_KEYS = ["choices", "effective", "override_profile_id", "source", "task_id"];

function parseProfileView(value: unknown): TaskManagerProfileView | null {
  if (!isRecord(value) || !text(value.task_id) || !text(value.source) || !Array.isArray(value.choices)) return null;
  if (value.override_profile_id !== null && !text(value.override_profile_id)) return null;
  if (!isRecord(value.effective)) return null;
  const effective = value.effective;
  if (!text(effective.profile_id) || !text(effective.model) || !text(effective.runtime) || !text(effective.provider)) return null;
  if (effective.effort !== null && effective.effort !== undefined && !text(effective.effort)) return null;
  const choices: TaskManagerProfileChoice[] = [];
  for (const row of value.choices) {
    if (!isRecord(row) || !text(row.profile_id) || !text(row.label) || !text(row.model) || !text(row.runtime)) return null;
    choices.push({ profile_id: row.profile_id, label: row.label, model: row.model, runtime: row.runtime });
  }
  return {
    task_id: value.task_id,
    override_profile_id: value.override_profile_id,
    effective: {
      profile_id: effective.profile_id,
      model: effective.model,
      runtime: effective.runtime,
      provider: effective.provider,
      effort: effective.effort ?? null,
    },
    source: value.source,
    choices,
  };
}

/** The server's closed view is proof when it names this task and the requested override. */
function profileViewConfirms(value: unknown, taskId: string, profileId: string | null): boolean {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value).sort();
  if (keys.length !== VIEW_KEYS.length || VIEW_KEYS.some((key, index) => keys[index] !== key)) return false;
  const view = parseProfileView(value);
  if (!view || view.task_id !== taskId || view.override_profile_id !== profileId) return false;
  return profileId === null || view.effective.profile_id === profileId;
}

function classify(err: V1Error): ProfileSetResult {
  if (isMissingOperation(err)) return { ok: false, reason: "missing" };
  const turnBusy = err.code === "conflict" && (err.message.includes("stop its turn") || err.message.includes("working in this task"));
  if (err.status === 409 && (err.code === "task_manager_busy" || turnBusy)) {
    return { ok: false, reason: "busy", message: err.message };
  }
  if (err.status === 400 && (err.code === "invalid" || err.code === "task_manager_runtime_unsupported")) {
    return { ok: false, reason: "invalid", message: err.message };
  }
  return { ok: false, reason: "failure", message: err.message };
}

export async function setTaskManagerProfile(
  taskId: string,
  profileId: string | null,
  signal?: AbortSignal,
): Promise<ProfileSetResult> {
  try {
    const result = await callV1<unknown>(
      TASK_MANAGER_PROFILE_SET_PATH,
      { task_id: taskId, profile_id: profileId },
      { signal },
    );
    if (isPending(result)) return { ok: false, reason: "uncertain" };
    const parsed = parseSetResult(result);
    if (parsed && parsed.task_id === taskId && parsed.profile_id === profileId) {
      return { ok: true, profile_id: parsed.profile_id };
    }
    if (profileViewConfirms(result, taskId, profileId)) return { ok: true, profile_id: profileId };
    return { ok: false, reason: "uncertain" };
  } catch (err) {
    if (signal?.aborted || isAbort(err)) throw err;
    if (err instanceof SessionEndedError) return { ok: false, reason: "failure", message: err.message };
    if (err instanceof V1Error) return classify(err);
    return { ok: false, reason: "uncertain" };
  }
}

/** Read the task's effective profile. A missing operation or a body that is not the view yields null. */
export async function getTaskManagerProfile(taskId: string, signal?: AbortSignal): Promise<TaskManagerProfileView | null> {
  try {
    const result = await callV1<unknown>(TASK_MANAGER_PROFILE_GET_PATH, { task_id: taskId }, { signal });
    if (isPending(result)) return null;
    const view = parseProfileView(result);
    if (!view || view.task_id !== taskId) return null;
    return view;
  } catch (err) {
    if (signal?.aborted || isAbort(err)) throw err;
    if (err instanceof V1Error && isMissingOperation(err)) return null;
    throw err;
  }
}
