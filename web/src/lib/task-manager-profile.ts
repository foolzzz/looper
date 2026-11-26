import { SessionEndedError } from "@/lib/session";
import { callV1, V1Error } from "@/lib/v1";
import { isMissingOperation } from "@/lib/work";

/**
 * Closed write from tasks.task_manager_profile_set: body and result are
 * exactly {task_id, profile_id}. There is no read operation for this profile.
 * Busy is HTTP 409 conflict. A runtime refusal is HTTP 400 invalid.
 * Both use the server's English message.
 */
export const TASK_MANAGER_PROFILE_SET_PATH = "/api/v1/tasks/task_manager_profile_set";

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

function classify(err: V1Error): ProfileSetResult {
  if (isMissingOperation(err)) return { ok: false, reason: "missing" };
  if (err.status === 409 && err.code === "conflict") {
    if (err.message.includes("stop its turn") || err.message.includes("working in this task")) {
      return { ok: false, reason: "busy", message: err.message };
    }
    return { ok: false, reason: "failure", message: err.message };
  }
  if (err.status === 400 && err.code === "invalid") return { ok: false, reason: "invalid", message: err.message };
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
    if (!parsed || parsed.task_id !== taskId || parsed.profile_id !== profileId) {
      return { ok: false, reason: "uncertain" };
    }
    return { ok: true, profile_id: parsed.profile_id };
  } catch (err) {
    if (signal?.aborted || isAbort(err)) throw err;
    if (err instanceof SessionEndedError) return { ok: false, reason: "failure", message: err.message };
    if (err instanceof V1Error) return classify(err);
    return { ok: false, reason: "uncertain" };
  }
}
