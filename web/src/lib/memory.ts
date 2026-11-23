import { callV1, confirmV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

/** JSON of the memory operations (`/api/v1/memory/*`). Unknown fields stay optional so a newer server still reads. */

export type MemoryState =
  | "drafting" | "pending" | "confirmed" | "writing" | "written" | "rejected" | "expired" | "conflict"
  | "blocked" | "dropped" | "held" | "in_effect" | "revoked" | "superseded";

export interface MemoryCandidate {
  id: string;
  kind: string;
  state: MemoryState | string;
  reason: string | null;
  destination: string;
  source_kind: string;
  source_class: string;
  statement: string;
  why: string;
  keywords: string[];
  title: string | null;
  note_path: string | null;
  revision: number;
  content_hash: string | null;
  untrusted: boolean;
  flagged: boolean;
  hard_rule: boolean;
  project_id: string | null;
  manager_id: string | null;
  commit: string | null;
  merged_into: string | null;
  supersedes: string | null;
  expires_at: string | null;
  valid_until: string | null;
  written_at: string | null;
  purged: boolean;
  sources_total: number;
  created_at: string;
  updated_at: string;
}

/** One change of a revision, as the writer applies it. */
export type MemoryChange =
  | { op: "create_note"; path: string; content: string }
  | { op: "replace_note"; path: string; base_sha256: string; content: string }
  | { op: "append_line"; path: string; under: string | null; line: string }
  | { op: "replace_line"; path: string; old: string; new: string };

export interface MemoryRevision {
  revision: number;
  made_by: string;
  title: string;
  changes: MemoryChange[];
  diff_stats: Record<string, { added?: number; removed?: number }>;
  large_removal: boolean;
  content_hash: string;
  created_at: string;
  /** A unified diff per path, made by the server from the vault file as it is now. */
  diffs?: Record<string, string>;
  /** Why `diffs` is empty although the revision has changes. */
  diffs_problem?: string | null;
}

export interface MemorySource {
  kind: string;
  source_class: string;
  quote: string | null;
  channel: string | null;
  chat: string | null;
  message_id: string | null;
  said_at: string | null;
  task_id: string | null;
  lead: string | null;
}

export interface MemoryEvent {
  action: string;
  actor: string;
  via: string;
  revision: number | null;
  content_hash: string | null;
  note: string | null;
  at: string;
}

export interface MemoryDetail extends MemoryCandidate {
  current: MemoryRevision | null;
  sources: MemorySource[];
  events: MemoryEvent[];
}

export interface MemoryList { candidates: MemoryCandidate[]; more: boolean }

export interface MemoryBatchItem {
  candidate: string;
  ok: boolean;
  result: { candidate: MemoryDetail } | null;
  error: { code: string; message: string } | null;
}

export interface MemoryTarget { shape: "note" | "entry"; folders?: string[]; index?: string | null; files?: string[] }

export interface MemorySettings {
  enabled: boolean;
  root: string;
  rules_files: string[];
  targets: Record<string, MemoryTarget>;
  required_frontmatter: string[];
  sources: { manager: boolean; tasks: boolean; leads: boolean };
  branch: string | null;
  worktree_root: string | null;
  commit_prefix: string | null;
  push_channel: string | null;
  pending_days: number;
  draft_budget_usd: number;
  daily_budget_usd: number;
}

export interface MemorySettingsView {
  configured: boolean;
  enabled: boolean;
  settings: Partial<MemorySettings> | null;
  problem: string | null;
}

export interface MemoryCheck {
  configured: boolean;
  enabled: boolean;
  ok: boolean;
  checks: { name: string; ok: boolean; detail: string }[];
}

/** What `settings_put` answers when it asks first: the impact text, and the call that runs it. */
export class MemoryNeedsConfirmation extends Error {
  readonly pending: V1PendingConfirmation;
  constructor(pending: V1PendingConfirmation) {
    super(pending.impact);
    this.name = "MemoryNeedsConfirmation";
    this.pending = pending;
  }
}

function isPending(value: unknown): value is V1PendingConfirmation {
  return Boolean(value) && typeof value === "object" && (value as { status?: string }).status === "needs_confirmation";
}

async function call<T>(operation: string, body: unknown = {}): Promise<T> {
  const result = await callV1<T>(`/api/v1/memory/${operation}`, body);
  // No memory action but settings_put asks first; an unexpected ask is shown, never confirmed on the operator's behalf.
  if (isPending(result)) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export const memoryList = (args: { state?: string; kind?: string; limit?: number; before?: string } = {}) =>
  call<MemoryList>("list", args);
export const memoryGet = async (candidate: string) => (await call<{ candidate: MemoryDetail }>("get", { candidate })).candidate;
/** `contentHash` is the hash of the revision the page rendered: a changed candidate answers 409. */
export const memoryConfirm = async (candidate: string, contentHash: string) =>
  (await call<{ candidate: MemoryDetail }>("confirm", { candidate, content_hash: contentHash })).candidate;
export const memoryConfirmMany = async (items: { candidate: string; content_hash: string }[]) =>
  (await call<{ items: MemoryBatchItem[] }>("confirm_many", { items })).items;
export const memoryReject = async (candidate: string, reason?: string) =>
  (await call<{ candidate: MemoryDetail }>("reject", { candidate, ...(reason ? { reason } : {}) })).candidate;
export const memoryEdit = async (candidate: string, contentHash: string, changes: MemoryChange[], confirm = false) =>
  (await call<{ candidate: MemoryDetail }>("edit", { candidate, content_hash: contentHash, changes, confirm })).candidate;
export const memoryRetry = async (candidate: string) => (await call<{ candidate: MemoryDetail }>("retry", { candidate })).candidate;
export const memoryRevive = async (candidate: string) => (await call<{ candidate: MemoryDetail }>("revive", { candidate })).candidate;
export const memoryDelete = (candidate: string) => call<{ candidate: string; deleted: boolean }>("delete", { candidate });
export const memorySettingsGet = () => call<MemorySettingsView>("settings_get");
export const memoryCheck = () => call<MemoryCheck>("check");

/** Saves the whole setting. A call that asks first throws `MemoryNeedsConfirmation`; `memoryConfirmSave` runs it. */
export async function memorySettingsPut(body: Record<string, unknown>): Promise<MemorySettingsView> {
  const result = await callV1<MemorySettingsView>("/api/v1/memory/settings_put", body);
  if (isPending(result)) throw new MemoryNeedsConfirmation(result);
  return result;
}

export async function memoryConfirmSave(pending: V1PendingConfirmation): Promise<MemorySettingsView> {
  const result = await confirmV1<MemorySettingsView>(pending.confirmation_id, pending.session);
  if (isPending(result)) throw new V1Error(202, "unavailable", result.impact);
  return result;
}
