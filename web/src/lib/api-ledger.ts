import { callV1, V1Error } from "@/lib/v1";
import type { MissingOperatorBot } from "@/lib/api";
export type LedgerKind = "pending" | "decision" | "work_item";
export type LedgerStatus = "open" | "done" | "cancelled" | "expired" | "superseded";
export type LedgerSection = "attention" | "work" | "recent_decisions" | "all";
export type LedgerActor = "user" | "relayed" | "system";
export type LedgerVia = "dashboard" | "lark" | "telegram" | "cli" | "api" | "system";
export interface LedgerRef { kind: "task" | "gate" | "ledger" | "intent" | "pr" | "doc" | "vault" | "url"; ref: string; label?: string; }
export interface LedgerItem {
  id: string; number: number; kind: LedgerKind; status: LedgerStatus | null; title: string; detail: string;
  assurance: LedgerActor; source: { kind: "gate" | "task" | "confirmation"; ref: string; state: string | null; href?: string | null; } | null;
  owner: string; project_id: string | null; due_at: string | null; overdue: boolean; refs: LedgerRef[];
  superseded_by: string | null; added_via: LedgerVia | null; created_at: string; updated_at: string; closed_at: string | null;
}
export interface LedgerEvent {
  id: number; action: "add" | "note" | "amend" | "confirm" | "close" | "cancel" | "supersede" | "reopen";
  actor_kind: LedgerActor; actor_id: string | null; relayed_by: string | null; via: LedgerVia;
  session_label: string | null; quote: string | null; note: string | null;
  changes: Record<string, unknown> | null; created_at: string;
}
export interface LedgerPageRequest {
  section?: LedgerSection; status?: LedgerStatus | "all"; kind?: LedgerKind; owner?: string; project_id?: string;
  limit?: number; cursor?: string;
}
export interface LedgerPageResult {
  items: LedgerItem[]; more: number; next_cursor: string | null; counts: Record<LedgerStatus, number>;
  attention_count: number; missing_operator_ids: MissingOperatorBot[];
}
export interface LedgerDetail { item: LedgerItem; events: LedgerEvent[]; }
export interface LedgerAdd {
  kind: LedgerKind; title: string; detail?: string; due?: string | null; owner?: string | null;
  project_id?: string | null; refs?: LedgerRef[]; quote?: string | null;
}
export interface LedgerAmend { title?: string; detail?: string | null; due?: string | null; owner?: string | null; project_id?: string | null; }
export type LedgerAction = { item: string; action: "confirm" | "reopen"; }
  | { item: string; action: "close" | "cancel"; note?: string; }
  | { item: string; action: "note"; note: string; }
  | { item: string; action: "amend"; fields: LedgerAmend; };
async function request<T>(name: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const result = await callV1<T>(`/api/v1/ledger/${name}`, body, { signal });
  if (result && typeof result === "object" && "status" in result)
    throw new V1Error(202, "unavailable", "账本操作未确认完成，请先刷新核实。");
  return result as T;
}

export const ledgerPage = (body: LedgerPageRequest = {}, signal?: AbortSignal) => request<LedgerPageResult>("page", body, signal);

export const ledgerGet = (item: string, signal?: AbortSignal) => request<LedgerDetail>("get", { item }, signal);

export async function ledgerAdd(body: LedgerAdd): Promise<LedgerItem> {
  const { kind, title, detail, due, owner, project_id, refs, quote } = body;
  return (await request<{ item: LedgerItem; }>("add", { kind, title, detail, due, owner, project_id, refs, quote })).item;
}

export async function ledgerAction(body: LedgerAction): Promise<LedgerItem> {
  const common = { item: body.item, action: body.action };
  const args = body.action === "amend" ? { ...common, fields: body.fields }
    : "note" in body ? { ...common, note: body.note } : common;
  return (await request<{ item: LedgerItem; }>("action", args)).item;
}
