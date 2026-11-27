import { callV1, V1Error } from "@/lib/v1";
import { isMissingOperation } from "@/lib/work";

/** projects.task_manager_sandbox_get sends {project} and answers the closed view below (the operator reads it). */
export const TASK_MANAGER_SANDBOX_GET_PATH = "/api/v1/projects/task_manager_sandbox_get";

export interface TaskManagerSandboxView {
  project: string;
  network_allowed_domains: string[];
  revision: number;
}

export type SandboxRead =
  | { ok: true; view: TaskManagerSandboxView }
  | { ok: false; reason: "missing" }
  | { ok: false; reason: "failure"; message: string };

function parseView(value: unknown, project: string): TaskManagerSandboxView | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const keys = Object.keys(row).sort();
  if (keys.length !== 3 || keys.join() !== "network_allowed_domains,project,revision") return null;
  const domains = row.network_allowed_domains;
  if (row.project !== project || !Array.isArray(domains) || !domains.every((item) => typeof item === "string")) return null;
  if (typeof row.revision !== "number" || !Number.isInteger(row.revision) || row.revision < 0) return null;
  return { project: row.project, network_allowed_domains: domains as string[], revision: row.revision };
}

/** Anything but the closed view naming this project is a failure, never an empty (nothing allowed) list. */
export async function readTaskManagerSandbox(project: string, signal?: AbortSignal): Promise<SandboxRead> {
  try {
    const result = await callV1<unknown>(TASK_MANAGER_SANDBOX_GET_PATH, { project }, { signal });
    const view = parseView(result, project);
    return view ? { ok: true, view } : { ok: false, reason: "failure", message: "服务器返回了无法识别的白名单。" };
  } catch (err) {
    if (signal?.aborted || (err instanceof Error && err.name === "AbortError")) throw err;
    if (isMissingOperation(err)) return { ok: false, reason: "missing" };
    return { ok: false, reason: "failure", message: err instanceof V1Error || err instanceof Error ? err.message : "读取失败" };
  }
}
