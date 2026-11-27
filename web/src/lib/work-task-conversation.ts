import { useEffect, useRef, useState } from "react";
import type { WorkFeedRecord } from "@/components/work-activity-feed";
import type {
  WorkConversationDetail,
  WorkConversationProps,
  WorkIdentity,
  WorkPaneMessage,
} from "@/components/work-conversation";
import {
  fetchChatHistory,
  ForeignConversationError,
  stopChatReply,
  streamChatThread,
  type ChatHistoryPage,
  type ChatTarget,
  type DbChatMessage,
  type SSEEvent,
  type TaskDetail,
  type TaskTurnState,
  type UserGate,
} from "@/lib/api";
import { historyFrom, joinStaged, pullBack } from "@/lib/composer";
import { randomId } from "@/lib/ids";
import { errorText } from "@/lib/use-action";
import { useReplyStream } from "@/lib/use-reply-stream";
import { useWorkConversationState } from "@/lib/use-work-conversation-state";

/** Inputs for one task thread. The page mounts this only while task conversations are on. */
export interface TaskConversationInput {
  taskId: string;
  detail?: WorkConversationDetail | null;
  task?: TaskDetail;
  onGateChanged?: (gate: UserGate) => void;
}

const UNREADABLE = "无法读取这条事件";
const INTERRUPTED = "这一轮被中断，内容已保留";
const STOPPING = "正在停止…";
const CONNECT_FAILED = "连接失败，请检查服务器是否运行。";
const STREAM_LOST = "回复流已断开，服务端可能仍在运行";
const INVALID_PAGE = "服务器返回了无效的对话分页。";
const COMMANDS = ["/stop", "/done", "/pause", "/background", "/takeover", "/start"] as const;
const QUEUED_REASONS = ["cap", "turn"] as const;
const SWITCH_REASONS = ["busy", "invalid_profile", "runtime_unsupported"] as const;
const FRESH_REASONS = ["context_bound", "read_only_open", "resume_failed"] as const;
const STOP_METHODS = ["control", "cancel", "kill"] as const;
const SUMMARY_MODES = ["direct", "orchestrated"] as const;
const REF_CHANGES = ["created", "deleted", "moved", "non_fast_forward", "fast_forward_unverified"] as const;
const SANDBOX_NOTICES = ["first_enable_in_place", "refs_unreadable"] as const;
const REFUSED = ["work_mode_off", "runtime_unsupported", "budget", "outbound_review_unavailable", "workspace", "shutting_down", "review_hook_disabled", "cli_unverified", "manager_answers_off", "mcp_unresolved", "sandbox_unavailable", "readback_unavailable", "launch_setting_mismatch", "mcp_set_mismatch", "mcp_launcher_unsafe", "network_policy_invalid", "workspace_not_repo_root", "workspace_is_home", "workspace_contains_trusted_config", "workspace_in_system_dir", "workspace_overlaps_session", "worktree_unsupported", "head_moved", "head_detached"] as const;

type RefChange = { ref: string; change: (typeof REF_CHANGES)[number]; old: string | null; new: string | null; restore: string };
type ToolPayload = { tool_use_id: string; name: string; args: string; result: string; is_error: boolean };
type TaskEvent =
  | { kind: "accepted"; reason: "duplicate" }
  | { kind: "starting"; status: string }
  | { kind: "command"; command: (typeof COMMANDS)[number] }
  | { kind: "queued"; reason: (typeof QUEUED_REASONS)[number]; position: number; active: number }
  | { kind: "model_switched"; from_profile_id: string; to_profile_id: string }
  | { kind: "model_switch_rejected"; profile_id: string | null; reason: (typeof SWITCH_REASONS)[number] }
  | { kind: "session_fresh"; reason: (typeof FRESH_REASONS)[number]; detail: string }
  | { kind: "stopped"; method: (typeof STOP_METHODS)[number] }
  | { kind: "turn_summary"; files: string[]; commit: string | null; mode: (typeof SUMMARY_MODES)[number]; uncommitted?: string[]; unverified?: string[] }
  | { kind: "ref_report"; message_id: string | null; report_id: number | null; changes: RefChange[]; info: RefChange[];
      head: { before: string | null; after: string | null } | null; own_commits: number; needs_you: boolean }
  | { kind: "sandbox_notice"; notice: (typeof SANDBOX_NOTICES)[number]; detail: string; message_id?: string | null }
  | { kind: "refused"; reason: "orchestrated"; status: string }
  | { kind: "refused"; reason: (typeof REFUSED)[number] };

interface Live {
  taskId: string;
  messages: WorkPaneMessage[];
  records: WorkFeedRecord[];
  eventKeys: Map<string, string>;
  history: WorkConversationProps["history"];
  historyError: string;
  phase: string;
  notice: string;
  replying: boolean;
  turnState?: TaskTurnState;
  earlier: "hidden" | "ready" | "loading" | "error";
  earlierError: string;
  stopping: boolean;
}

interface ParsedPage {
  messages: WorkPaneMessage[];
  records: WorkFeedRecord[];
  keys: Map<string, string>;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isString = (value: unknown): value is string => typeof value === "string";
const isInteger = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value);
const oneOf = <T extends string>(value: unknown, options: readonly T[]): value is T =>
  typeof value === "string" && (options as readonly string[]).includes(value);
const SUMMARY_KEYS = ["kind", "files", "commit", "mode"] as const;
const SUMMARY_OPTIONAL = ["uncommitted", "unverified"] as const;
const exactKeys = (value: Record<string, unknown>, expected: readonly string[]) => {
  const got = Object.keys(value);
  return got.length === expected.length && expected.every((key) => got.includes(key));
};

/** Canonical JSON so a live event and its persisted copy share one identity. */
function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => stable(item)).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function parseContent(content: string): unknown {
  try {
    return JSON.parse(content) as unknown;
  } catch {
    return undefined;
  }
}

function asTool(value: unknown): ToolPayload | null {
  if (!isRecord(value) || !exactKeys(value, ["tool_use_id", "name", "args", "result", "is_error"])) return null;
  if (!isString(value.tool_use_id) || value.tool_use_id === "" || !isString(value.name) || !isString(value.args)
    || !isString(value.result) || typeof value.is_error !== "boolean") return null;
  return { tool_use_id: value.tool_use_id, name: value.name, args: value.args, result: value.result, is_error: value.is_error };
}

const isNullableString = (value: unknown): value is string | null => value === null || isString(value);

function asRefChanges(value: unknown): RefChange[] | null {
  if (!Array.isArray(value)) return null;
  const found: RefChange[] = [];
  for (const item of value) {
    if (!isRecord(item) || !exactKeys(item, ["ref", "change", "old", "new", "restore"]) || !isString(item.ref)
      || !oneOf(item.change, REF_CHANGES) || !isNullableString(item.old) || !isNullableString(item.new)
      || !isString(item.restore)) return null;
    found.push({ ref: item.ref, change: item.change, old: item.old, new: item.new, restore: item.restore });
  }
  return found;
}

function asEvent(value: unknown): TaskEvent | null {
  if (!isRecord(value) || !isString(value.kind)) return null;
  switch (value.kind) {
    case "accepted":
      return exactKeys(value, ["kind", "reason"]) && value.reason === "duplicate" ? { kind: "accepted", reason: "duplicate" } : null;
    case "starting":
      return exactKeys(value, ["kind", "status"]) && isString(value.status) ? { kind: "starting", status: value.status } : null;
    case "command":
      return exactKeys(value, ["kind", "command"]) && oneOf(value.command, COMMANDS) ? { kind: "command", command: value.command } : null;
    case "queued":
      return exactKeys(value, ["kind", "reason", "position", "active"]) && oneOf(value.reason, QUEUED_REASONS)
        && isInteger(value.position) && isInteger(value.active) && value.active >= 0
        ? { kind: "queued", reason: value.reason, position: value.position, active: value.active } : null;
    case "model_switched":
      return exactKeys(value, ["kind", "from_profile_id", "to_profile_id"]) && isString(value.from_profile_id) && isString(value.to_profile_id)
        ? { kind: "model_switched", from_profile_id: value.from_profile_id, to_profile_id: value.to_profile_id } : null;
    case "model_switch_rejected":
      return exactKeys(value, ["kind", "profile_id", "reason"]) && (value.profile_id === null || isString(value.profile_id))
        && oneOf(value.reason, SWITCH_REASONS)
        ? { kind: "model_switch_rejected", profile_id: value.profile_id, reason: value.reason } : null;
    case "session_fresh":
      return exactKeys(value, ["kind", "reason", "detail"]) && oneOf(value.reason, FRESH_REASONS) && isString(value.detail)
        ? { kind: "session_fresh", reason: value.reason, detail: value.detail } : null;
    case "stopped":
      return exactKeys(value, ["kind", "method"]) && oneOf(value.method, STOP_METHODS) ? { kind: "stopped", method: value.method } : null;
    case "turn_summary":
      if (!exactKeys(value, [...SUMMARY_KEYS, ...SUMMARY_OPTIONAL.filter((key) => key in value)])
        || !Array.isArray(value.files) || !value.files.every(isString)
        || !(value.commit === null || isString(value.commit)) || !oneOf(value.mode, SUMMARY_MODES)) return null;
      if (!SUMMARY_OPTIONAL.every((key) => !(key in value) || (Array.isArray(value[key]) && (value[key] as unknown[]).every(isString)))) return null;
      return { kind: "turn_summary", files: value.files, commit: value.commit, mode: value.mode,
        ...("uncommitted" in value ? { uncommitted: value.uncommitted as string[] } : {}),
        ...("unverified" in value ? { unverified: value.unverified as string[] } : {}) };
    case "ref_report": {
      if (!exactKeys(value, ["kind", "message_id", "report_id", "changes", "info", "head", "own_commits", "needs_you"])
        || !isNullableString(value.message_id)) return null;
      const changes = asRefChanges(value.changes);
      const info = asRefChanges(value.info);
      const head = value.head;
      const headOk = head === null || (isRecord(head) && exactKeys(head, ["before", "after"])
        && isNullableString(head.before) && isNullableString(head.after));
      if (!changes || !info || !headOk || !(value.report_id === null || isInteger(value.report_id))
        || !isInteger(value.own_commits) || value.own_commits < 0 || typeof value.needs_you !== "boolean") return null;
      return { kind: "ref_report", message_id: value.message_id, report_id: value.report_id as number | null, changes, info,
        head: head as { before: string | null; after: string | null } | null, own_commits: value.own_commits,
        needs_you: value.needs_you };
    }
    case "sandbox_notice":
      return exactKeys(value, ["kind", "notice", "detail", ..."message_id" in value ? ["message_id"] : []])
        && oneOf(value.notice, SANDBOX_NOTICES) && isString(value.detail)
        && (!("message_id" in value) || isNullableString(value.message_id))
        ? { kind: "sandbox_notice", notice: value.notice, detail: value.detail,
            ...("message_id" in value ? { message_id: value.message_id as string | null } : {}) } : null;
    case "refused":
      if (exactKeys(value, ["kind", "reason", "status"]) && value.reason === "orchestrated" && isString(value.status)) {
        return { kind: "refused", reason: "orchestrated", status: value.status };
      }
      return exactKeys(value, ["kind", "reason"]) && oneOf(value.reason, REFUSED) ? { kind: "refused", reason: value.reason } : null;
    default:
      return null;
  }
}

const CHANGE_TEXT: Record<RefChange["change"], string> = {
  created: "新建", deleted: "被删除", moved: "被移动", non_fast_forward: "被改写（不是快进）",
  fast_forward_unverified: "无法确认是否为快进",
};
const shortId = (id: string | null) => (id ? id.slice(0, 8) : "无");
const NON_FAST_FORWARD_WARNING = "执行 reset --hard、rebase 或 commit --amend 之后恢复这个引用，工作区会留下一份撤销那些提交的差异；不要不看就提交它。";
const FAST_FORWARD_UNVERIFIED_WARNING = "没能确认这是不是快进（提交历史太长或对象读不了），所以不能说它被重置过；请自己检查，确认是改写后再用恢复命令。";
const FIRST_ENABLE_TEXT = "管家在你的真实检出目录里直接改文件；v1 只报告分支和标签的变化，不会恢复，删掉的对象也找不回来，建议先把重要分支推送或备份。";

const REFS_UNREADABLE_TEXT = "本轮结束后没能重新读取分支和标签，这一轮对它们的改动（如果有）没有被报告；请自己检查 git branch 和 git tag。";

function refChangeText(change: RefChange): string {
  const warning = change.change === "non_fast_forward" ? ` ${NON_FAST_FORWARD_WARNING}`
    : change.change === "fast_forward_unverified" ? ` ${FAST_FORWARD_UNVERIFIED_WARNING}` : "";
  return `${change.ref} ${CHANGE_TEXT[change.change]}（${shortId(change.old)} → ${shortId(change.new)}）${warning}`;
}

function refReportText(event: Extract<TaskEvent, { kind: "ref_report" }>): string {
  const parts = ["本轮之后引用有变化，Looper 不会自动恢复，下面的恢复命令由你自己决定是否执行："];
  parts.push(...event.changes.map(refChangeText).map((line) => `- ${line}`));
  parts.push(...event.info.map((change) => `- ${refChangeText(change)}（属于另一个 Looper 任务）`));
  if (event.needs_you) parts.push("这些变化已列入「需要你处理」。");
  if (event.head) {
    parts.push(`HEAD 从 ${event.head.before ?? "detached"} 变成 ${event.head.after ?? "detached"}，不会自动切回；`
      + "下一轮需要把任务分支重新检出，或改记录的任务分支。");
  }
  if (event.own_commits > 0) parts.push(`本轮有 ${event.own_commits} 个提交由任务管家自己做。`);
  return parts.join("\n");
}

/** The commands of an event, shown in code font under its text. */
function taskEventCode(event: TaskEvent): string[] {
  return event.kind === "ref_report" ? [...event.changes, ...event.info].map((change) => change.restore) : [];
}

function taskEventText(event: TaskEvent): string {
  switch (event.kind) {
    case "queued":
      return `排队中：已有 ${event.active} 个任务管家在工作`;
    case "model_switched":
      return `模型已切换：${event.from_profile_id} → ${event.to_profile_id}`;
    case "model_switch_rejected":
      return event.profile_id ? `模型未切换（${event.reason}：${event.profile_id}）` : `模型未切换（${event.reason}）`;
    case "session_fresh":
      return `${event.detail}（${event.reason}）`;
    case "accepted":
      return "已接受（duplicate）";
    case "starting":
      return event.status;
    case "command":
      return event.command;
    case "stopped":
      return `已停止（${event.method}）`;
    case "turn_summary": {
      const files = event.files.length ? ` · ${event.files.join("、")}` : "";
      const commit = event.commit ? ` · ${event.commit}` : "";
      const kept = event.uncommitted?.length ? ` · 以下文件在本轮开始前就有未提交改动，未自动提交：${event.uncommitted.join("、")}` : "";
      const unsure = event.unverified?.length ? ` · 以下文件无法确认是否为本轮前已有的改动（文件过大或数量过多），未自动提交：${event.unverified.join("、")}` : "";
      return `${event.mode}${files}${commit}${kept}${unsure}`;
    }
    case "ref_report":
      return refReportText(event);
    case "sandbox_notice":
      return event.notice === "first_enable_in_place" ? FIRST_ENABLE_TEXT : REFS_UNREADABLE_TEXT;
    case "refused":
      return "status" in event ? `已拒绝（${event.reason}：${event.status}）` : `已拒绝（${event.reason}）`;
    default:
      return UNREADABLE;
  }
}

const codeOf = (event: TaskEvent) => {
  const code = taskEventCode(event);
  return code.length ? { code } : {};
};

function unreadable(id: string, createdAt?: string): WorkFeedRecord {
  return { source: "task-event", id, ...(createdAt ? { created_at: createdAt } : {}), kind: "unreadable", text: UNREADABLE };
}

function toolRecord(tool: ToolPayload, createdAt?: string): Extract<WorkFeedRecord, { source: "tool" }> {
  const pending = tool.result === "" && !tool.is_error;
  return {
    source: "tool",
    id: tool.tool_use_id,
    ...(createdAt ? { created_at: createdAt } : {}),
    name: tool.name,
    args: tool.args,
    ...(pending ? {} : { result: tool.result, isError: tool.is_error }),
  };
}

/** A later empty result does not wipe a tool row that already finished. */
function upsertTool(records: WorkFeedRecord[], tool: ToolPayload, createdAt?: string) {
  const next = toolRecord(tool, createdAt);
  const index = records.findIndex((record) => record.source === "tool" && record.id === tool.tool_use_id);
  if (index < 0) {
    records.push(next);
    return;
  }
  const previous = records[index];
  if (previous.source === "tool" && previous.result !== undefined && next.result === undefined) return;
  records[index] = { ...previous, ...next, created_at: previous.created_at ?? next.created_at };
}

function withStopped(messages: WorkPaneMessage[]): WorkPaneMessage[] {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role !== "assistant") continue;
    if (messages[index].outcome === "stopped") return messages;
    const next = messages.slice();
    next[index] = { ...messages[index], outcome: "stopped" };
    return next;
  }
  return messages;
}

function badKey(payload: unknown): string {
  return `bad:${stable(payload)}`;
}

function readTaskHistory(rows: DbChatMessage[]): ParsedPage {
  const messages: WorkPaneMessage[] = [];
  const records: WorkFeedRecord[] = [];
  const keys = new Map<string, string>();
  const pushBad = (id: string, createdAt: string | undefined, key: string) => {
    if (keys.has(key)) return;
    keys.set(key, id);
    records.push(unreadable(id, createdAt));
  };
  for (const row of rows) {
    if (row.role === "user" || row.role === "assistant") {
      messages.push({ id: row.message_id, role: row.role, content: row.content, created_at: row.created_at });
      continue;
    }
    const parsed = parseContent(row.content);
    if (row.role === "tool") {
      const tool = asTool(parsed);
      if (!tool) pushBad(row.message_id, row.created_at, parsed === undefined ? `bad-text:${row.content}` : badKey(parsed));
      else upsertTool(records, tool, row.created_at);
      continue;
    }
    if (row.role === "event") {
      const event = asEvent(parsed);
      if (!event) {
        pushBad(row.message_id, row.created_at, parsed === undefined ? `bad-text:${row.content}` : badKey(parsed));
        continue;
      }
      if (event.kind === "model_switched" && event.from_profile_id === event.to_profile_id) continue;
      const canonical = stable(event);
      if (!keys.has(canonical)) {
        keys.set(canonical, row.message_id);
        records.push({ source: "task-event", id: row.message_id, created_at: row.created_at, kind: event.kind, text: taskEventText(event), ...codeOf(event) });
      }
      if (event.kind === "stopped") {
        const marked = withStopped(messages);
        messages.splice(0, messages.length, ...marked);
      }
      continue;
    }
    pushBad(row.message_id, row.created_at, parsed === undefined ? `bad-text:${row.content}` : badKey(parsed));
  }
  return { messages, records, keys };
}

function foldLive(records: WorkFeedRecord[], keys: Map<string, string>, kind: string, payload: unknown): {
  records: WorkFeedRecord[];
  keys: Map<string, string>;
  stopped: boolean;
} {
  if (kind === "agent.tool") {
    const tool = asTool(payload);
    if (!tool) return addBad(records, keys, payload);
    const next = records.slice();
    upsertTool(next, tool);
    return { records: next, keys, stopped: false };
  }
  if (kind !== "task.event") return { records, keys, stopped: false };
  const event = asEvent(payload);
  if (!event) return addBad(records, keys, payload);
  if (event.kind === "model_switched" && event.from_profile_id === event.to_profile_id) {
    return { records, keys, stopped: false };
  }
  const canonical = stable(event);
  const existing = keys.get(canonical);
  if (existing && !existing.startsWith("live:") && !existing.startsWith("unreadable:")) {
    return { records, keys, stopped: event.kind === "stopped" };
  }
  const id = existing ?? `live:${canonical}`;
  const nextKeys = new Map(keys);
  nextKeys.set(canonical, id);
  if (records.some((record) => record.id === id)) return { records, keys: nextKeys, stopped: event.kind === "stopped" };
  return {
    records: [...records, { source: "task-event", id, kind: event.kind, text: taskEventText(event), ...codeOf(event) }],
    keys: nextKeys,
    stopped: event.kind === "stopped",
  };
}

function addBad(records: WorkFeedRecord[], keys: Map<string, string>, payload: unknown): {
  records: WorkFeedRecord[];
  keys: Map<string, string>;
  stopped: boolean;
} {
  const key = badKey(payload);
  if (keys.has(key)) return { records, keys, stopped: false };
  const id = `unreadable:${stable(payload)}`;
  const nextKeys = new Map(keys);
  nextKeys.set(key, id);
  return { records: [...records, unreadable(id)], keys: nextKeys, stopped: false };
}

/** History wins over a live copy of the same payload. A newer tool result wins over an older pending row. */
function mergeHistory(liveRecords: WorkFeedRecord[], liveKeys: Map<string, string>, page: ParsedPage): {
  records: WorkFeedRecord[];
  keys: Map<string, string>;
} {
  const keys = new Map(liveKeys);
  const replaced = new Set<string>();
  for (const [key, id] of page.keys) {
    const existing = keys.get(key);
    if (existing && (existing.startsWith("live:") || existing.startsWith("unreadable:"))) replaced.add(existing);
    if (!existing || existing.startsWith("live:") || existing.startsWith("unreadable:")) keys.set(key, id);
  }
  const records = page.records.slice();
  for (const live of liveRecords) {
    if (replaced.has(live.id)) continue;
    if (live.source === "tool") {
      const index = records.findIndex((record) => record.source === "tool" && record.id === live.id);
      if (index < 0) records.push(live);
      else if (live.result !== undefined && records[index]?.source === "tool" && records[index].result === undefined) {
        records[index] = { ...records[index], result: live.result, isError: live.isError, name: live.name };
      }
      continue;
    }
    if (!records.some((record) => record.id === live.id)) records.push(live);
  }
  return { records, keys };
}

/** Older rows fill gaps. A tool already on the newer page stays as the newer page left it. */
function prependRecords(current: WorkFeedRecord[], currentKeys: Map<string, string>, page: ParsedPage): {
  records: WorkFeedRecord[];
  keys: Map<string, string>;
} {
  const keys = new Map(currentKeys);
  const records = current.slice();
  for (const [key, id] of page.keys) {
    const existing = keys.get(key);
    if (!existing) {
      keys.set(key, id);
      const record = page.records.find((item) => item.id === id);
      if (record) records.push(record);
    } else if ((existing.startsWith("live:") || existing.startsWith("unreadable:")) && !id.startsWith("live:") && !id.startsWith("unreadable:")) {
      keys.set(key, id);
      const index = records.findIndex((item) => item.id === existing);
      const record = page.records.find((item) => item.id === id);
      if (index >= 0 && record) records[index] = { ...record, created_at: records[index]?.created_at ?? record.created_at };
    }
  }
  for (const record of page.records) {
    if (record.source === "tool" && !records.some((item) => item.source === "tool" && item.id === record.id)) records.push(record);
  }
  return { records, keys };
}

function prependMessages(current: WorkPaneMessage[], older: WorkPaneMessage[]): WorkPaneMessage[] {
  const seen = new Set(current.map((message) => message.id));
  return [...older.filter((message) => !seen.has(message.id)), ...current];
}

function taskTurn(page: ChatHistoryPage): TaskTurnState {
  if (page.turn_state !== "idle" && page.turn_state !== "running" && page.turn_state !== "interrupted") {
    throw new Error(INVALID_PAGE);
  }
  if (typeof page.has_more !== "boolean" || (page.next_before !== null && typeof page.next_before !== "string")) {
    throw new Error(INVALID_PAGE);
  }
  if (page.has_more && page.next_before === null) throw new Error(INVALID_PAGE);
  return page.turn_state;
}

function sendFailure(error: unknown): string {
  if (error instanceof TypeError || !(error instanceof Error)) return CONNECT_FAILED;
  return error.message;
}

function blank(taskId: string, cached?: { messages: WorkPaneMessage[]; messagesLoaded: boolean }): Live {
  const messages = cached?.messages ?? [];
  return {
    taskId,
    messages,
    records: [],
    eventKeys: new Map(),
    history: cached?.messagesLoaded ? (messages.length ? "ready" : "empty") : "loading",
    historyError: "",
    phase: "",
    notice: "",
    replying: false,
    earlier: "hidden",
    earlierError: "",
    stopping: false,
  };
}

/**
 * One task thread, `task-chat:<task id>`, sent to `{kind:'task', task_id}`.
 * Turn state, paging and model-switch hints come from the server values only.
 */
export function useWorkTaskConversation({
  taskId,
  detail = null,
  task,
  onGateChanged,
}: TaskConversationInput): WorkConversationProps {
  const sessionId = `task-chat:${taskId}`;
  const identity: WorkIdentity = { kind: "task", taskId, sessionId };
  const taskRef = useRef(taskId);
  taskRef.current = taskId;
  const gen = useRef(0);
  const genTask = useRef(taskId);
  const nextBefore = useRef<string | null>(null);
  const loadingEarlier = useRef(false);
  if (genTask.current !== taskId) {
    genTask.current = taskId;
    gen.current += 1;
    nextBefore.current = null;
    loadingEarlier.current = false;
  }
  const reply = useReplyStream(() => ({ id: taskRef.current, threadId: `task-chat:${taskRef.current}` }));
  const replyRef = useRef(reply);
  replyRef.current = reply;
  const conversation = useWorkConversationState(identity);
  const { store, key: cacheKey } = conversation;
  const cached = store.cached(cacheKey);
  const [live, setLive] = useState(() => blank(taskId, cached));
  if (live.taskId !== taskId) setLive(blank(taskId, cached));
  const liveRef = useRef(live);
  liveRef.current = live.taskId === taskId ? live : blank(taskId, cached);
  const shownDraft = conversation.value.draft;
  const shownStaged = conversation.value.staged;
  const draftRef = useRef(shownDraft);
  const stagedRef = useRef(shownStaged);
  draftRef.current = shownDraft;
  stagedRef.current = shownStaged;
  const setDraft = (next: string | ((value: string) => string)) => {
    const value = typeof next === "function" ? next(draftRef.current) : next;
    draftRef.current = value;
    conversation.setDraft(value);
  };
  const setStaged = (next: string[]) => {
    stagedRef.current = next;
    conversation.setStaged(next);
  };
  const [retry, setRetry] = useState(0);
  const sendGen = useRef(0);
  const finalSeen = useRef(false);
  const sending = useRef(false);
  const sendRef = useRef<(text: string) => void>(() => {});

  useEffect(() => () => { replyRef.current.cancel(); }, [taskId]);

  useEffect(() => {
    const ctrl = new AbortController();
    const turn = gen.current;
    const sent = sendGen.current;
    setLive((current) => current.taskId !== taskId ? current : {
      ...current,
      history: store.cached(cacheKey)?.messagesLoaded ? current.history : "loading",
      historyError: "",
    });
    fetchChatHistory(sessionId, { signal: ctrl.signal }).then((page) => {
      if (ctrl.signal.aborted || turn !== gen.current || taskRef.current !== taskId || sent !== sendGen.current) return;
      const state = taskTurn(page);
      const parsed = readTaskHistory(page.messages);
      nextBefore.current = page.has_more ? page.next_before : null;
      setLive((current) => {
        if (current.taskId !== taskId) return current;
        const merged = mergeHistory(current.records, current.eventKeys, parsed);
        const running = state === "running";
        const historyStopped = parsed.records.some((record) => record.source === "task-event" && record.kind === "stopped");
        const messages = !historyStopped && merged.records.some((record) => record.source === "task-event" && record.kind === "stopped")
          ? withStopped(parsed.messages) : parsed.messages;
        return {
          ...current,
          messages,
          records: merged.records,
          eventKeys: merged.keys,
          history: parsed.messages.length === 0 && merged.records.length === 0 ? "empty" : "ready",
          historyError: "",
          turnState: state,
          replying: running || sending.current,
          stopping: running ? current.stopping : false,
          notice: running || current.notice !== STOPPING ? current.notice : "",
          earlier: page.has_more ? "ready" : "hidden",
          earlierError: "",
        };
      });
    }).catch((error: unknown) => {
      if (ctrl.signal.aborted || turn !== gen.current || taskRef.current !== taskId) return;
      if (error instanceof Error && error.name === "AbortError") return;
      const reason = error instanceof ForeignConversationError ? error.message : errorText(error);
      setLive((current) => current.taskId !== taskId ? current : {
        ...current,
        history: "error",
        historyError: `${current.messages.length ? "刷新失败：" : ""}${reason}`,
      });
    });
    return () => ctrl.abort();
  }, [taskId, sessionId, retry, store, cacheKey]);

  useEffect(() => {
    const turn = gen.current;
    return streamChatThread(sessionId, (event) => {
      if (turn !== gen.current || taskRef.current !== taskId) return;
      if (event.kind !== "agent.tool" && event.kind !== "task.event") return;
      if (event.kind === "task.event" && asEvent(event.payload)?.kind === "stopped") sending.current = false;
      setLive((current) => {
        if (current.taskId !== taskId) return current;
        const folded = foldLive(current.records, current.eventKeys, event.kind, event.payload);
        const ended = folded.stopped && !sending.current;
        return {
          ...current,
          records: folded.records,
          eventKeys: folded.keys,
          messages: folded.stopped ? withStopped(current.messages) : current.messages,
          replying: ended ? false : current.replying,
          stopping: ended ? false : current.stopping,
          notice: ended && current.notice === STOPPING ? "" : current.notice,
        };
      });
    });
  }, [taskId, sessionId]);

  useEffect(() => {
    if (live.taskId !== taskId || live.history === "loading") return;
    store.cache(cacheKey, { messages: live.messages });
    const history = historyFrom(live.messages);
    if (history.length) conversation.rememberHistory(history);
  }, [live, taskId, store, cacheKey]);

  const restoreDraft = (extra: string[] = []) => {
    const list = [...extra, ...stagedRef.current];
    if (list.length === 0) return;
    setDraft((value) => pullBack(list, value));
    setStaged([]);
  };

  const applyStream = (current: Live, event: SSEEvent, assistantId: string): Live => {
    if (event.kind === "agent.tool" || event.kind === "task.event") {
      const folded = foldLive(current.records, current.eventKeys, event.kind, event.payload);
      return {
        ...current,
        records: folded.records,
        eventKeys: folded.keys,
        messages: folded.stopped ? withStopped(current.messages) : current.messages,
        replying: folded.stopped ? false : current.replying,
        stopping: folded.stopped ? false : current.stopping,
        notice: folded.stopped && current.notice === STOPPING ? "" : current.notice,
      };
    }
    if (event.kind === "answer.delta") {
      const chunk = String(event.payload.text ?? "");
      return {
        ...current,
        messages: current.messages.map((message) => message.id === assistantId
          ? { ...message, content: message.content + chunk } : message),
      };
    }
    if (event.kind === "agent.phase") return { ...current, phase: String(event.payload.label ?? "") };
    return current;
  };

  const send = (text: string) => {
    if (!text.trim() || taskRef.current !== taskId) return;
    const turn = gen.current;
    const target: ChatTarget = { kind: "task", task_id: taskId };
    sendGen.current += 1;
    finalSeen.current = false;
    sending.current = true;
    reply.cancel();
    const id = randomId();
    const assistantId = `${id}:reply`;
    const createdAt = new Date().toISOString();
    setLive((current) => ({
      ...current,
      taskId,
      history: current.history === "loading" || current.history === "empty" ? "ready" : current.history,
      notice: "",
      phase: "",
      replying: true,
      messages: [
        ...current.messages,
        { id, role: "user", content: text, created_at: createdAt },
        { id: assistantId, role: "assistant", content: "", streaming: true, created_at: createdAt },
      ],
    }));
    const still = { current: () => false };
    reply.start({
      target,
      threadId: sessionId,
      messageId: id,
      text,
      ownerId: taskId,
      isCurrent: still,
      onEvent: (event) => {
        if (turn !== gen.current || taskRef.current !== taskId || !still.current()) return;
        if (event.kind === "task.event" && asEvent(event.payload)?.kind === "stopped") sending.current = false;
        if (event.kind === "answer.final") {
          finalSeen.current = true;
          sending.current = false;
          const finalText = String(event.payload.text ?? "");
          const outcome = String(event.payload.outcome ?? "answered");
          setLive((current) => ({
            ...current,
            phase: "",
            replying: false,
            stopping: false,
            notice: current.notice === STOPPING ? "" : current.notice,
            messages: current.messages.map((message) => message.id === assistantId ? {
              ...message,
              content: finalText || message.content || "已完成。",
              streaming: false,
              outcome: outcome === "stopped" || message.outcome === "stopped" ? "stopped" : undefined,
            } : message),
          }));
          const queued = stagedRef.current;
          if (queued.length === 0) return;
          if (outcome === "answered") {
            setStaged([]);
            sendRef.current(joinStaged(queued));
          } else restoreDraft();
          return;
        }
        setLive((current) => current.taskId === taskId ? applyStream(current, event, assistantId) : current);
      },
      onDone: () => {
        if (turn !== gen.current || taskRef.current !== taskId || !still.current()) return;
        const disconnected = !finalSeen.current;
        sending.current = false;
        setLive((current) => ({
          ...current,
          replying: false,
          stopping: false,
          phase: "",
          notice: disconnected ? STREAM_LOST : current.notice === STOPPING ? "" : current.notice,
          messages: current.messages.map((message) => message.id === assistantId ? { ...message, streaming: false } : message),
        }));
        if (disconnected) restoreDraft();
      },
      onError: (error) => {
        if (turn !== gen.current || taskRef.current !== taskId || !still.current()) return;
        sending.current = false;
        setLive((current) => ({
          ...current,
          replying: false,
          phase: "",
          notice: sendFailure(error),
          messages: current.messages.filter((message) => message.id !== id && message.id !== assistantId),
        }));
        restoreDraft([text]);
      },
    });
  };
  sendRef.current = send;

  const stop = () => {
    const turn = gen.current;
    const current = liveRef.current;
    if (current.taskId !== taskId || current.stopping || !current.replying) return;
    const user = [...current.messages].reverse().find((message) => message.role === "user");
    if (!user) return;
    setLive((state) => state.taskId === taskId ? { ...state, stopping: true, notice: STOPPING } : state);
    void stopChatReply(user.id).then((result) => {
      if (turn !== gen.current || taskRef.current !== taskId) return;
      const accepted = result.state === "stopping" || result.state === "already_stopping";
      setLive((state) => state.taskId !== taskId ? state : {
        ...state,
        stopping: accepted,
        notice: accepted ? STOPPING : result.state,
      });
    }).catch((error: unknown) => {
      if (turn !== gen.current || taskRef.current !== taskId) return;
      setLive((state) => state.taskId !== taskId ? state : { ...state, stopping: false, notice: errorText(error) });
    });
  };

  const loadEarlier = () => {
    const before = nextBefore.current;
    if (!before || loadingEarlier.current) return;
    const turn = gen.current;
    loadingEarlier.current = true;
    setLive((current) => current.taskId === taskId ? { ...current, earlier: "loading", earlierError: "" } : current);
    fetchChatHistory(sessionId, { before }).then((page) => {
      if (turn !== gen.current || taskRef.current !== taskId) return;
      const state = taskTurn(page);
      const parsed = readTaskHistory(page.messages);
      nextBefore.current = page.has_more ? page.next_before : null;
      setLive((current) => {
        if (current.taskId !== taskId) return current;
        const merged = prependRecords(current.records, current.eventKeys, parsed);
        const running = state === "running" || sending.current;
        return {
          ...current,
          messages: prependMessages(current.messages, parsed.messages),
          records: merged.records,
          eventKeys: merged.keys,
          turnState: state,
          replying: running,
          stopping: state === "running" ? current.stopping : false,
          notice: state === "running" || current.notice !== STOPPING ? current.notice : "",
          earlier: page.has_more ? "ready" : "hidden",
          earlierError: "",
        };
      });
    }).catch((error: unknown) => {
      if (turn !== gen.current || taskRef.current !== taskId) return;
      setLive((current) => current.taskId !== taskId ? current : {
        ...current,
        earlier: "error",
        earlierError: errorText(error),
      });
    }).finally(() => {
      if (turn === gen.current) loadingEarlier.current = false;
    });
  };

  const view = live.taskId === taskId ? live : liveRef.current;
  const stopId = view.replying ? [...view.messages].reverse().find((message) => message.role === "user")?.id : undefined;
  return {
    identity,
    canSend: true,
    detail,
    task,
    onGateChanged,
    messages: view.messages,
    records: view.records,
    history: view.history,
    historyError: view.historyError,
    onRetry: () => setRetry((value) => value + 1),
    phase: view.phase,
    replying: view.replying,
    draft: shownDraft,
    onDraft: setDraft,
    staged: shownStaged,
    onStaged: setStaged,
    inputHistory: conversation.value.history,
    onSend: (text) => {
      setDraft("");
      send(text);
    },
    onStop: () => { stop(); },
    notice: view.notice || undefined,
    canStop: view.replying && !!stopId && !view.stopping,
    earlier: view.earlier,
    earlierError: view.earlierError,
    onLoadEarlier: loadEarlier,
    turnState: view.turnState,
    banner: view.turnState === "interrupted" ? INTERRUPTED : undefined,
  };
}
