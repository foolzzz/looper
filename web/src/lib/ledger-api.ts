import { callV1, V1Error } from "@/lib/v1";

export type LedgerKind = "pending" | "decision" | "work_item";
export type LedgerStatus = "open" | "done" | "cancelled" | "expired" | "superseded";

export interface LedgerItem {
  id: string;
  number: number;
  kind: LedgerKind;
  status: LedgerStatus | null;
  title: string;
  detail: string;
  /** Who stands behind it: the user, a relayed voice, or the system (a sourced item). */
  assurance: "user" | "relayed" | "system";
  source: { kind: "gate" | "confirmation" | "task"; ref: string; state: string | null } | null;
  owner: string;
  project_id: string | null;
  due_at: string | null;
  overdue: boolean;
  refs: { kind: string; ref: string; label?: string }[];
  superseded_by: string | null;
  added_via: string | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

export interface LedgerEvent {
  id: number;
  action: string;
  actor_kind: "user" | "relayed" | "system";
  actor_id: string | null;
  relayed_by: string | null;
  via: string;
  session_label: string | null;
  quote: string | null;
  note: string | null;
  changes: Record<string, [unknown, unknown]> | null;
  created_at: string;
}

export interface LedgerCounts {
  open: number; done: number; cancelled: number; expired: number; superseded: number;
}

export interface LedgerState {
  enabled: boolean;
  needs_you: number;
  counts: LedgerCounts;
  operator_unset: boolean;
}

export type LedgerAction = "confirm" | "close" | "cancel" | "reopen" | "note" | "amend";

async function op<T extends object>(name: string, body: object = {}, signal?: AbortSignal): Promise<T> {
  const result = await callV1<T>(`/api/v1/ledger/${name}`, body, { signal });
  if ("status" in result && "impact" in result) throw new V1Error(202, "unavailable", String(result.impact));
  return result;
}

export const fetchLedgerState = (signal?: AbortSignal) => op<LedgerState>("status", {}, signal);
export const configureLedger = (enabled: boolean) => op<LedgerState>("configure", { enabled });
export const fetchLedgerItems = (
  filters: { status?: string; kind?: LedgerKind; needs_you?: boolean; limit?: number },
  signal?: AbortSignal,
) => op<{ items: LedgerItem[]; more: number; counts: LedgerCounts }>("list", filters, signal);
export const fetchLedgerItem = (item: string, signal?: AbortSignal) =>
  op<{ item: LedgerItem; events: LedgerEvent[] }>("get", { item }, signal);
export const actOnLedgerItem = (
  item: string,
  action: LedgerAction,
  extra: { note?: string; fields?: { title?: string; detail?: string; due?: string | null } } = {},
) => op<LedgerItem>("act", { item, action, ...extra });
export const addLedgerItem = (fields: { kind: LedgerKind; title: string; detail?: string; due?: string | null }) =>
  op<LedgerItem>("add", fields);
