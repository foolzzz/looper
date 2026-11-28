// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManagerWorkConversation, TaskWorkConversation } from "@/components/work-conversation";
import { clearWorkConversationMemory } from "@/lib/use-work-conversation-state";
import type { DbChatMessage, TaskDetail } from "@/lib/api";
import { ForeignConversationError } from "@/lib/api";
import { V1Error } from "@/lib/v1";
import { WorkPage } from "@/pages/work";

type Handler = (event: { kind: string; payload: Record<string, unknown> }) => void;

const api = vi.hoisted(() => ({
  fetchChatMessages: vi.fn(),
  fetchChatHistory: vi.fn(),
  fetchChatTargets: vi.fn(),
  sendChat: vi.fn(),
  streamChatReply: vi.fn(),
  streamChatThread: vi.fn(),
  stopChatReply: vi.fn(),
  fetchChatTurn: vi.fn(),
  fetchChatSettings: vi.fn(),
  watchChat: vi.fn(),
  fetchTaskActivityWindow: vi.fn(),
  streamTaskActivity: vi.fn(),
}));
const bridge = vi.hoisted(() => ({ real: null as typeof import("@/lib/api") | null }));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  bridge.real = real;
  return {
    ...real,
    fetchChatMessages: api.fetchChatMessages,
    fetchChatHistory: api.fetchChatHistory,
    fetchChatTargets: api.fetchChatTargets,
    sendChat: api.sendChat,
    streamChatReply: api.streamChatReply,
    streamChatThread: api.streamChatThread,
    stopChatReply: api.stopChatReply,
    fetchChatTurn: api.fetchChatTurn,
    fetchChatSettings: api.fetchChatSettings,
    watchChat: api.watchChat,
    fetchTaskActivityWindow: api.fetchTaskActivityWindow,
    streamTaskActivity: api.streamTaskActivity,
  };
});
vi.mock("@/lib/use-projects", () => ({ useProjects: () => ({ projects: [] }) }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const idle = (messages: DbChatMessage[], extra: Record<string, unknown> = {}) => ({
  messages, has_more: false, next_before: null, turn_state: "idle", ...extra,
});
const row = (messageId: string, role: string, content: string, createdAt: string): DbChatMessage => ({
  message_id: messageId, role, content, created_at: createdAt,
});
const taskOf = (taskId: string) => ({
  task_id: taskId, status: "completed", group: "completed", gates: [], steps: [],
  created_at: "2026-11-25T00:00:00Z", project_id: "p", project_name: "Beta",
} as unknown as TaskDetail);

function geometry() {
  let height = 120;
  const size = () => 60;
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => height);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function scrollHeight(this: HTMLElement) {
    return [...this.querySelectorAll<HTMLElement>("[data-feed-key]")].reduce((total) => total + size(), 0);
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function rect(this: HTMLElement) {
    const viewport = this.closest('[aria-label="对话历史"]') as HTMLElement | null;
    const rows = viewport ? [...viewport.querySelectorAll<HTMLElement>("[data-feed-key]")] : [];
    const index = rows.indexOf(this);
    const top = index < 0 ? 0 : index * size() - viewport!.scrollTop;
    return { top, bottom: top + size(), height: size(), left: 0, right: 600, width: 600, x: 0, y: top, toJSON() {} };
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  return { hide: () => { height = 0; } };
}

describe("task conversation", () => {
  let container: HTMLDivElement;
  let root: Root;
  let threadEmit: Handler;
  let replyEmit: Handler;

  const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  const input = () => container.querySelector("textarea") as HTMLTextAreaElement;
  const type = (text: string) => act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(input(), text);
    input().dispatchEvent(new Event("input", { bubbles: true }));
  });
  const key = (name: string) => act(() => {
    input().dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true }));
  });
  const say = async (text: string) => {
    type(text);
    key("Enter");
    for (let attempt = 0; attempt < 6; attempt += 1) await flush();
  };
  async function show(node: ReactNode) {
    await act(async () => { root.render(node); });
    await flush();
  }
  const button = (label: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === label);
  const stopButton = () => container.querySelector('[aria-label="停止回复（Esc）"]');

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    clearWorkConversationMemory();
    threadEmit = () => {};
    replyEmit = () => {};
    for (const fn of Object.values(api)) fn.mockReset();
    api.fetchChatHistory.mockResolvedValue(idle([]));
    api.fetchChatMessages.mockResolvedValue([]);
    api.fetchChatTargets.mockResolvedValue([]);
    api.fetchTaskActivityWindow.mockResolvedValue({ events: [], hasMore: false });
    api.streamTaskActivity.mockImplementation(() => () => {});
    api.streamChatThread.mockImplementation((_session: string, onEvent: Handler) => {
      threadEmit = onEvent;
      return () => {};
    });
    api.streamChatReply.mockImplementation((_id: string, onEvent: Handler) => {
      replyEmit = onEvent;
      return () => {};
    });
    api.sendChat.mockResolvedValue({ correlation_id: "reply-correlation" });
    api.stopChatReply.mockResolvedValue({ state: "stopping", method: "control" });
    api.fetchChatSettings.mockResolvedValue({ stop_enabled: false, completion_turns: "off", ledger_enabled: false });
    api.fetchChatTurn.mockResolvedValue(null);
    api.watchChat.mockImplementation(() => () => {});
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    if (root) act(() => root.unmount());
    container?.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each([
    [400, "no task t1"],
    [404, "missing task"],
    [409, "work mode is off"],
  ])("keeps the task draft when send fails with %s and does not resend to the Planner", async (status, message) => {
    api.sendChat.mockRejectedValue(new V1Error(status, status === 409 ? "conflict" : "invalid", message));
    await show(<TaskWorkConversation taskId="t1" />);
    await say("hello");
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(api.sendChat.mock.calls[0]?.[1]).toBe("task-chat:t1");
    expect(api.sendChat.mock.calls[0]?.[3]).toEqual({ kind: "task", task_id: "t1" });
    expect(api.sendChat.mock.calls.some((call) => (call[3] as { kind?: string } | undefined)?.kind === "planner")).toBe(false);
    expect(api.fetchChatTargets).not.toHaveBeenCalled();
    expect(input().value).toBe("hello");
    expect(container.textContent).toContain(message);
  });

  it("keeps the draft when the task send fails on the network", async () => {
    api.sendChat.mockRejectedValue(new TypeError("Failed to fetch"));
    await show(<TaskWorkConversation taskId="t1" />);
    await say("hello");
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(api.sendChat.mock.calls[0]?.[3]).toEqual({ kind: "task", task_id: "t1" });
    expect(input().value).toBe("hello");
    expect(container.textContent).toContain("连接失败，请检查服务器是否运行。");
  });

  it("updates one tool row from pending to result and keeps that id after history reload", async () => {
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    const tool = (result: string, isError: boolean) => ({
      tool_use_id: "tu1", name: "bash", args: "{}", result, is_error: isError,
    });
    act(() => threadEmit({ kind: "agent.tool", payload: tool("", false) }));
    await flush();
    expect(container.querySelectorAll('[data-feed-key="tool:tu1"]')).toHaveLength(1);
    expect(container.querySelector('[data-feed-key="tool:tu1"]')?.textContent).toContain("进行中");
    act(() => threadEmit({ kind: "agent.tool", payload: tool("ok", false) }));
    await flush();
    expect(container.querySelectorAll('[data-feed-key="tool:tu1"]')).toHaveLength(1);
    expect(container.querySelector('[data-feed-key="tool:tu1"]')?.textContent).toContain("完成");
    act(() => threadEmit({ kind: "agent.tool", payload: tool("", false) }));
    await flush();
    expect(container.querySelector('[data-feed-key="tool:tu1"]')?.textContent).toContain("完成");
    act(() => threadEmit({ kind: "agent.tool", payload: { tool_use_id: "tu2", name: "bash", args: "{}", result: "", is_error: true } }));
    await flush();
    expect(container.querySelector('[data-feed-key="tool:tu2"]')?.textContent).toContain("失败");

    api.fetchChatHistory.mockResolvedValue(idle([
      row("reply:tool:tu1", "tool", JSON.stringify(tool("ok", false)), "2026-11-25T00:00:02Z"),
    ]));
    await show(<TaskWorkConversation key="again" taskId="t1" task={taskOf("t1")} />);
    await flush();
    expect(container.querySelectorAll('[data-feed-key="tool:tu1"]')).toHaveLength(1);
    expect(container.querySelector('[data-feed-key="tool:reply:tool:tu1"]')).toBeNull();
    expect(container.querySelector('[data-feed-key="tool:tu1"]')?.textContent).toContain("完成");
  });

  it("shows the closed task-event union and leaves unknown records unreadable", async () => {
    const events: Record<string, unknown>[] = [
      { kind: "session_fresh", reason: "context_bound", detail: "The task session reached its context bound." },
      { kind: "session_fresh", reason: "read_only_open", detail: "branch_gone" },
      { kind: "session_fresh", reason: "resume_failed", detail: "Claude Code resumed a different session" },
      { kind: "model_switched", from_profile_id: "a", to_profile_id: "b" },
      { kind: "model_switched", from_profile_id: "same", to_profile_id: "same" },
      { kind: "model_switch_rejected", profile_id: null, reason: "busy" },
      { kind: "model_switch_rejected", profile_id: "profile-a", reason: "invalid_profile" },
      { kind: "model_switch_rejected", profile_id: "profile-b", reason: "runtime_unsupported" },
      { kind: "queued", reason: "cap", position: 2, active: 4 },
      { kind: "accepted", reason: "duplicate" },
      { kind: "starting", status: "status:booting" },
      { kind: "command", command: "/pause" },
      { kind: "stopped", method: "control" },
      { kind: "turn_summary", files: ["src/a.ts"], commit: "abc123", mode: "direct" },
      { kind: "turn_summary", files: ["src/b.ts"], commit: null, mode: "direct", uncommitted: ["src/b.ts"] },
      { kind: "turn_summary", files: ["src/c.ts"], commit: null, mode: "direct", unverified: ["src/c.ts"] },
      { kind: "refused", reason: "orchestrated", status: "running" },
      ...["work_mode_off", "runtime_unsupported", "budget", "outbound_review_unavailable", "workspace", "shutting_down", "review_hook_disabled", "cli_unverified", "manager_answers_off", "mcp_unresolved", "sandbox_unavailable", "readback_unavailable", "launch_setting_mismatch", "mcp_set_mismatch", "mcp_launcher_unsafe", "network_policy_invalid", "workspace_not_repo_root", "workspace_is_home", "workspace_contains_trusted_config", "workspace_in_system_dir", "workspace_overlaps_session", "worktree_unsupported", "head_moved", "head_detached"].map((reason) => ({ kind: "refused", reason })),
      { kind: "queued", reason: "cap", position: 1, active: 6, type: "queued" },
      { kind: "sandbox_denial", tool_use_id: "tu-a", category: "network", case: "not_allowed", target: "blocked.example:443", client: null, command_name: "curl" },
      { kind: "sandbox_denial", tool_use_id: "tu-b", category: "network", case: "unsupported_client", target: "api.example:443", client: "go_tls", command_name: "gh" },
      { kind: "sandbox_denial", tool_use_id: "tu-c", category: "network", case: "unsupported_client", target: null, client: "go_tls", command_name: "terraform" },
      { kind: "sandbox_denial", tool_use_id: "tu-d", category: "other", case: "unknown", target: null, client: null, command_name: "ls" },
      { kind: "sandbox_denial", tool_use_id: "tu-e", category: "write", case: "not_allowed", target: "/var/data/x", client: null, command_name: "touch" },
      { kind: "sandbox_denial", tool_use_id: "tu-g", category: "network", case: "not_allowable", target: "10.0.0.1:443", client: null, command_name: "curl" },
      { kind: "sandbox_denial", tool_use_id: "tu-f", category: "network", case: "unknown", target: "blocked.example:443", client: null, command_name: "x" },
    ];
    const messages = [
      row("asst", "assistant", "模型已切换：a → b", "2026-11-25T00:00:00Z"),
      row("user", "user", "go", "2026-11-25T00:00:01Z"),
      row("note", "note", "模型已切换：hidden", "2026-11-25T00:00:02Z"),
      ...events.map((event, index) => row(`e${index}`, "event", JSON.stringify(event), `2026-11-25T00:00:${String(index + 3).padStart(2, "0")}Z`)),
    ];
    api.fetchChatHistory.mockResolvedValue(idle(messages));
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    const text = container.textContent ?? "";
    expect(text).toContain("The task session reached its context bound.（context_bound）");
    expect(text).toContain("branch_gone（read_only_open）");
    expect(text).toContain("Claude Code resumed a different session（resume_failed）");
    expect(text).toContain("model_switched · 模型已切换：a → b");
    expect(text).not.toContain("model_switched · 模型已切换：same → same");
    expect(container.querySelectorAll('[data-feed-key^="task-event:"]').length).toBe(events.length);
    expect(text).toContain("以下文件在本轮开始前就有未提交改动，未自动提交：src/b.ts");
    expect(text).toContain("以下文件无法确认是否为本轮前已有的改动（文件过大或数量过多），未自动提交：src/c.ts");
    expect(text).toContain("模型未切换（busy）");
    expect(text).toContain("模型未切换（invalid_profile：profile-a）");
    expect(text).toContain("模型未切换（runtime_unsupported：profile-b）");
    expect(text).toContain("排队中：已有 4 个任务管家在工作");
    expect(text).not.toContain("已有 6");
    expect(text).toContain("已接受（duplicate）");
    expect(text).toContain("status:booting");
    expect(text).toContain("direct · src/a.ts · abc123");
    expect(text).toContain("已拒绝（orchestrated：running）");
    expect(text).toContain("已拒绝（budget）");
    expect(text).toContain("已拒绝（manager_answers_off）");
    expect(text).toContain("已拒绝（mcp_unresolved）");
    for (const reason of ["sandbox_unavailable", "readback_unavailable", "launch_setting_mismatch", "mcp_set_mismatch", "mcp_launcher_unsafe", "network_policy_invalid", "workspace_not_repo_root", "workspace_is_home", "workspace_contains_trusted_config", "workspace_in_system_dir", "workspace_overlaps_session", "worktree_unsupported", "head_moved", "head_detached"]) {
      expect(text).toContain(`已拒绝（${reason}）`);
    }
    expect(text).toContain("命令 curl 访问 `blocked.example:443` 被拒绝；该域名不在本项目的白名单里，操作者可在项目的任务管家沙箱设置里放行");
    expect(text).toContain("gh 这类命令在 v1 的管家会话里不受支持，放行域名不会让它可用");
    expect(text).toContain("terraform 这类命令在 v1 的管家会话里不受支持，放行域名不会让它可用");
    // A known client is unsupported whether or not the host is on the allowlist, so the row never claims it is listed.
    const unsupportedClient = [...container.querySelectorAll('[data-feed-key^="task-event:"]')]
      .filter((item) => item.textContent?.includes("这类命令在 v1 的管家会话里不受支持"));
    expect(unsupportedClient).toHaveLength(2);
    for (const item of unsupportedClient) {
      expect(item.textContent).not.toContain("在白名单里");
    }
    expect(text).toContain("命令 touch 写 `/var/data/x` 被拒绝：管家会话只能写任务目录");
    expect(text).toContain("命令 curl 访问 `10.0.0.1:443` 被拒绝：v1 不能放行这类目的地");
    const notAllowable = [...container.querySelectorAll('[data-feed-key^="task-event:"]')].find((row) => row.textContent?.includes("10.0.0.1:443"));
    expect(notAllowable?.textContent).not.toContain("操作者可在项目的任务管家沙箱设置里放行");
    expect(text).toContain("被沙箱拒绝（other）");
    // An unknown denial names no host and does not suggest that allowing one helps.
    const unknown = [...container.querySelectorAll('[data-feed-key^="task-event:"]')].find((row) => row.textContent?.includes("被沙箱拒绝（network）"));
    expect(unknown).toBeDefined();
    expect(unknown?.textContent).not.toContain("blocked.example");
    expect(unknown?.textContent).not.toContain("放行");
    expect(text).toContain("无法读取这条事件");
    expect(text).not.toContain("模型已切换：hidden");
    expect(container.querySelector('[data-role="assistant"]')?.textContent).toContain("已停止");
    const command = container.querySelector('[data-feed-key="task-event:e11"]');
    expect(command?.textContent).toContain("/pause");
    expect(command?.querySelector("button")).toBeNull();
    expect(container.querySelector('[data-role="assistant"]')?.textContent).toContain("模型已切换：a → b");
  });

  it("explains an unsupported client without claiming an unlisted host is allowlisted", async () => {
    const denial = {
      kind: "sandbox_denial", tool_use_id: "tu-unlisted", category: "network",
      case: "unsupported_client", target: "unlisted.example:443", client: "go_tls", command_name: "gh",
    };
    api.fetchChatHistory.mockResolvedValue(idle([
      row("e1", "event", JSON.stringify(denial), "2026-11-25T00:00:00Z"),
    ]));
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    const item = container.querySelector('[data-feed-key="task-event:e1"]');
    expect(item?.textContent).toContain("gh 这类命令在 v1 的管家会话里不受支持，放行域名不会让它可用");
    expect(item?.textContent).not.toContain("在白名单里");
    expect(item?.textContent).not.toContain("unlisted.example");
  });

  it("renders ref reports and the first-enable notice, and rejects malformed variants", async () => {
    const old = "a".repeat(40), moved = "b".repeat(40);
    const change = (ref: string, kind: string, from: string | null, to: string | null, restore: string) =>
      ({ ref, change: kind, old: from, new: to, restore });
    const good = [
      { kind: "sandbox_notice", notice: "first_enable_in_place", detail: "server detail" },
      { kind: "sandbox_notice", notice: "refs_unreadable", detail: "server detail" },
      { kind: "ref_report", message_id: "turn-1", report_id: 7, own_commits: 2, needs_you: true,
        head: { before: "refs/heads/main", after: null }, info: [change("refs/heads/looper/t2/task", "moved", old, moved, "git update-ref refs/heads/looper/t2/task A B")],
        changes: [change("refs/heads/oth\\u202eer", "moved", old, moved, `git update-ref refs/heads/other ${old} ${moved}`),
          change("refs/heads/main", "non_fast_forward", moved, old, `git update-ref refs/heads/main ${moved} ${old}`),
          change("refs/heads/big", "fast_forward_unverified", old, moved, `git update-ref refs/heads/big ${old} ${moved}`)] },
    ];
    const bad = [
      { kind: "sandbox_notice", notice: "other", detail: "x" },
      { kind: "ref_report", report_id: "7", own_commits: 0, needs_you: false, head: null, info: [], changes: [] },
      { kind: "ref_report", report_id: null, own_commits: 0, needs_you: false, head: null, info: [],
        changes: [change("refs/heads/x", "renamed", null, null, "x")] },
    ];
    const messages = [...good, ...bad].map((event, index) =>
      row(`r${index}`, "event", JSON.stringify(event), `2026-11-25T00:00:0${index}Z`));
    api.fetchChatHistory.mockResolvedValue(idle(messages));
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    const text = container.textContent ?? "";
    expect(text).toContain("管家在你的真实检出目录里直接改文件；v1 只报告分支和标签的变化，不会恢复，删掉的对象也找不回来，建议先把重要分支推送或备份。");
    expect(text).toContain("本轮结束后没能重新读取分支和标签");
    const report = container.querySelector('[data-feed-key="task-event:r2"]')!;
    expect(report.textContent).toContain("refs/heads/oth\\u202eer 被移动（aaaaaaaa → bbbbbbbb）");
    expect(report.textContent).toContain("refs/heads/main 被改写（不是快进）");
    expect(report.textContent).toContain("refs/heads/big 无法确认是否为快进（aaaaaaaa → bbbbbbbb）");
    expect(report.textContent).toContain("不能说它被重置过");
    expect(report.textContent).not.toMatch(/refs\/heads\/big 被改写/);
    expect(report.textContent).toContain("reset --hard、rebase 或 commit --amend");
    expect(report.textContent).toContain("属于另一个 Looper 任务");
    expect(report.textContent).toContain("已列入「需要你处理」");
    expect(report.textContent).toContain("HEAD 从 refs/heads/main 变成 detached，不会自动切回");
    expect(report.textContent).toContain("本轮有 2 个提交由任务管家自己做");
    expect(Array.from(report.querySelectorAll("code"), (node) => node.textContent)).toEqual([
      `git update-ref refs/heads/other ${old} ${moved}`, `git update-ref refs/heads/main ${moved} ${old}`,
      `git update-ref refs/heads/big ${old} ${moved}`, "git update-ref refs/heads/looper/t2/task A B"]);
    for (const index of [3, 4, 5]) expect(container.querySelector(`[data-feed-key="task-event:r${index}"]`)?.textContent).toContain("无法读取这条事件");
  });

  it("shows a shared restore command once and says why a change has none", async () => {
    const shared = "printf '%s\\n' 'symref-delete refs/heads/a refs/heads/b' | git update-ref --no-deref --stdin";
    const entry = (ref: string, restore: string | null, extra: object = {}) =>
      ({ ref, change: "moved", old: null, new: null, restore, ...extra });
    const report = (changes: object[]) => ({ kind: "ref_report", message_id: "turn-1", report_id: 8, own_commits: 0,
      needs_you: true, head: null, info: [], changes });
    const good = report([entry("refs/heads/a", shared), entry("refs/heads/b", shared),
      entry("refs/heads/via", null, { not_restorable: "chain_outside_refs" })]);
    const bad = [report([entry("refs/heads/x", null)]),
      report([entry("refs/heads/x", "x", { not_restorable: "git_too_old" })]),
      report([entry("refs/heads/x", null, { not_restorable: "other" })])];
    api.fetchChatHistory.mockResolvedValue(idle([good, ...bad].map((event, index) =>
      row(`s${index}`, "event", JSON.stringify(event), `2026-11-25T00:00:0${index}Z`))));
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    const shown = container.querySelector('[data-feed-key="task-event:s0"]')!;
    expect(Array.from(shown.querySelectorAll("code"), (node) => node.textContent)).toEqual([shared]);
    expect(shown.textContent).toContain("执行一次即可");
    expect(shown.textContent).toContain("不能给出恢复命令：引用链经过 refs/ 之外的名字");
    for (const index of [1, 2, 3]) {
      expect(container.querySelector(`[data-feed-key="task-event:s${index}"]`)?.textContent).toContain("无法读取这条事件");
    }
  });

  it("keeps repeated identical sandbox notices of different turns apart, and folds one turn's copy", async () => {
    const notice = (messageId: string) => ({ kind: "sandbox_notice", notice: "refs_unreadable", detail: "d", message_id: messageId });
    api.fetchChatHistory.mockResolvedValue(idle([
      row("n1", "event", JSON.stringify(notice("turn-1")), "2026-11-25T00:00:01Z"),
      row("n2", "event", JSON.stringify(notice("turn-2")), "2026-11-25T00:00:02Z"),
    ]));
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    expect(container.querySelectorAll('[data-feed-key^="task-event:"]')).toHaveLength(2);
    act(() => threadEmit({ kind: "task.event", payload: notice("turn-2") }));
    await flush();
    expect(container.querySelectorAll('[data-feed-key^="task-event:"]')).toHaveLength(2);
  });

  it("shows a live model switch without sending, and reloads it as the same row", async () => {
    const payload = { kind: "model_switched", from_profile_id: "a", to_profile_id: "b" };
    api.fetchChatHistory.mockResolvedValue(idle([
      row("ev1", "event", JSON.stringify(payload), "2026-11-25T00:00:01Z"),
    ]));
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    expect(api.sendChat).not.toHaveBeenCalled();
    act(() => threadEmit({ kind: "task.event", payload }));
    await flush();
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(container.querySelectorAll('[data-feed-key="task-event:ev1"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-feed-key^="task-event:"]')).toHaveLength(1);
    expect(container.textContent).toContain("模型已切换：a → b");
  });

  it("does not start a second turn when the server queues the task", async () => {
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    act(() => threadEmit({ kind: "task.event", payload: { kind: "queued", reason: "turn", position: 1, active: 4 } }));
    await flush();
    expect(container.textContent).toContain("排队中：已有 4 个任务管家在工作");
    expect(api.sendChat).not.toHaveBeenCalled();
    await say("go");
    act(() => replyEmit({ kind: "task.event", payload: { kind: "queued", reason: "cap", position: 2, active: 3 } }));
    await flush();
    expect(api.sendChat).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("排队中：已有 3 个任务管家在工作");
  });

  it.each([
    ["idle", "idle", false],
    ["running", "running", false],
    ["interrupted", "interrupted", true],
  ] as const)("reads %s from the server turn_state", async (_name, state, banner) => {
    api.fetchChatHistory.mockResolvedValue(idle([
      row("a", "assistant", "done", "2026-11-25T00:00:00Z"),
      row("u", "user", "still there", "2026-11-25T00:00:01Z"),
    ], { turn_state: state }));
    await show(<TaskWorkConversation taskId="t1" />);
    await flush();
    expect(container.querySelector("[data-turn-state]")?.getAttribute("data-turn-state")).toBe(state);
    expect((container.textContent ?? "").includes("这一轮被中断，内容已保留")).toBe(banner);
    expect(container.textContent).not.toContain("仅显示最近的对话");
    expect(!!stopButton()).toBe(state === "running");
  });

  it.each([
    ["missing turn_state", { messages: [], has_more: false, next_before: null }],
    ["missing has_more", { messages: [], next_before: null, turn_state: "idle" }],
    ["has_more without a cursor", { messages: [], has_more: true, next_before: null, turn_state: "idle" }],
  ])("reports an invalid task page when %s", async (_name, page) => {
    api.fetchChatHistory.mockResolvedValue(page);
    await show(<TaskWorkConversation taskId="t1" />);
    await flush();
    expect(container.textContent).toContain("服务器返回了无效的对话分页。");
    expect(container.querySelector("[data-turn-state]")).toBeNull();
    expect(container.textContent).not.toContain("这一轮被中断，内容已保留");
  });

  it("stops a running task without reading chat.stop_enabled", async () => {
    api.fetchChatHistory.mockResolvedValue(idle([
      row("user-1", "user", "hello", "2026-11-25T00:00:00Z"),
      row("user-1:reply", "assistant", "working", "2026-11-25T00:00:01Z"),
    ], { turn_state: "running" }));
    let resolveStop: (value: { state: string; method: string | null }) => void = () => {};
    api.stopChatReply.mockImplementation(() => new Promise((resolve) => { resolveStop = resolve; }));
    await show(<TaskWorkConversation taskId="t1" />);
    await flush();
    expect(stopButton()).not.toBeNull();
    expect(api.fetchChatSettings).not.toHaveBeenCalled();
    type("keep me");
    act(() => { (stopButton() as HTMLButtonElement).click(); });
    await flush();
    expect(container.textContent).toContain("正在停止…");
    expect(stopButton()).toBeNull();
    expect(input().value).toBe("keep me");
    await act(async () => { resolveStop({ state: "stopping", method: "control" }); });
    await flush();
    expect(api.stopChatReply).toHaveBeenCalledWith("user-1");
    expect(container.textContent).toContain("正在停止…");
    expect(container.textContent).not.toContain("已停止");
    expect(input().value).toBe("keep me");
  });

  it("keeps the draft and does not mark the turn stopped when stop fails", async () => {
    api.fetchChatHistory.mockResolvedValue(idle([
      row("user-1", "user", "hello", "2026-11-25T00:00:00Z"),
      row("user-1:reply", "assistant", "working", "2026-11-25T00:00:01Z"),
    ], { turn_state: "running" }));
    api.stopChatReply.mockRejectedValue(new Error("stop failed"));
    await show(<TaskWorkConversation taskId="t1" />);
    await flush();
    type("keep me");
    act(() => { (stopButton() as HTMLButtonElement).click(); });
    await flush();
    expect(container.textContent).toContain("stop failed");
    expect(container.textContent).not.toContain("已停止");
    expect(input().value).toBe("keep me");
    expect(api.fetchChatSettings).not.toHaveBeenCalled();
  });

  it("shows a resolved stop conflict and does not mark the turn stopped", async () => {
    api.fetchChatHistory.mockResolvedValue(idle([
      row("user-1", "user", "hello", "2026-11-25T00:00:00Z"),
    ], { turn_state: "running" }));
    api.stopChatReply.mockResolvedValue({ state: "cannot_interrupt", method: null });
    await show(<TaskWorkConversation taskId="t1" />);
    await flush();
    act(() => { (stopButton() as HTMLButtonElement).click(); });
    await flush();
    expect(container.textContent).toContain("cannot_interrupt");
    expect(container.textContent).not.toContain("已停止");
    expect(container.querySelector("[data-turn-state]")?.getAttribute("data-turn-state")).toBe("running");
  });

  it("keeps Manager stop behind chat.stop_enabled", async () => {
    localStorage.setItem("looper-chat-session-id", "sess-m");
    api.fetchChatTurn.mockResolvedValue({
      message_id: "m-turn", client_message_id: null, channel: "web", started_at: "2026-11-25T00:00:00Z",
      state: "running", method: null, stopping: false, can_interrupt: true,
    });
    await show(<ManagerWorkConversation />);
    for (let attempt = 0; attempt < 8 && !input().placeholder.includes("正在回复"); attempt += 1) await flush();
    expect(api.fetchChatSettings).toHaveBeenCalled();
    expect(input().placeholder).toContain("正在回复");
    expect(stopButton()).toBeNull();
    expect(api.stopChatReply).not.toHaveBeenCalled();
  });

  it("pages with the opaque cursor, retries a failed page, and dedupes the overlap", async () => {
    const stamp = "2026-11-25T00:00:02Z";
    const first = [row("m2", "user", "two", stamp), row("m3", "assistant", "three", stamp)];
    const older = [row("m1", "user", "one", "2026-11-25T00:00:01Z"), row("m2", "user", "two", stamp)];
    let earlier = 0;
    api.fetchChatHistory.mockImplementation(async (_session: string, options?: { before?: string }) => {
      if (options?.before) {
        earlier += 1;
        if (earlier === 1) throw new Error("page failed");
        return idle(older);
      }
      return idle(first, { has_more: true, next_before: "v1.cursor" });
    });
    await show(<TaskWorkConversation taskId="t1" />);
    await flush();
    expect(button("加载更早对话")).toBeTruthy();
    expect(button("加载更早对话")?.closest("[data-feed-key]")).toBeNull();
    act(() => button("加载更早对话")!.click());
    await flush();
    expect(container.textContent).toContain("更早的对话读取失败");
    expect(container.textContent).toContain("two");
    expect(container.textContent).toContain("three");
    expect(button("加载更早对话")?.hasAttribute("disabled")).toBe(false);
    act(() => button("加载更早对话")!.click());
    await flush();
    expect(container.querySelectorAll('[data-message-id="m2"]')).toHaveLength(1);
    expect(container.textContent).toContain("one");
    expect(button("加载更早对话")).toBeUndefined();
    const cursors = api.fetchChatHistory.mock.calls.map((call) => (call[1] as { before?: string } | undefined)?.before).filter(Boolean);
    expect(cursors).toEqual(["v1.cursor", "v1.cursor"]);
  });

  it("keeps the scroll anchor when an earlier page is prepended", async () => {
    geometry();
    const messages = ["m1", "m2", "m3", "m4", "m5"].map((id, index) =>
      row(id, "assistant", id, `2026-11-25T00:00:0${index + 1}Z`));
    api.fetchChatHistory.mockImplementation(async (_session: string, options?: { before?: string }) => {
      if (options?.before) return idle([row("earlier", "assistant", "earlier", "2026-11-25T00:00:00Z")]);
      return idle(messages, { has_more: true, next_before: "v1.cursor" });
    });
    await show(<TaskWorkConversation taskId="t1" />);
    for (let attempt = 0; attempt < 8 && !button("加载更早对话"); attempt += 1) await flush();
    const viewport = container.querySelector('[aria-label="对话历史"]') as HTMLElement;
    expect(button("加载更早对话")?.hasAttribute("data-feed-key")).toBe(false);
    viewport.scrollTop = 72;
    act(() => viewport.dispatchEvent(new Event("scroll")));
    act(() => button("加载更早对话")!.click());
    await flush();
    expect(container.textContent).toContain("earlier");
    expect(viewport.scrollTop).toBe(132);
  });

  it("shows a foreign thread without leaving the task or sending to the Planner", async () => {
    api.fetchChatHistory.mockRejectedValue(new ForeignConversationError("thread belongs to another manager", "mgr-b"));
    await show(<TaskWorkConversation taskId="t1" />);
    await flush();
    expect(container.textContent).toContain("thread belongs to another manager");
    expect(container.querySelector("[data-identity]")?.getAttribute("data-identity")).toBe("task:t1");
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(api.fetchChatMessages).not.toHaveBeenCalled();
  });

  it("drops a late history page after the selected task changes", async () => {
    let resolveA: (value: ReturnType<typeof idle>) => void = () => {};
    api.fetchChatHistory.mockImplementation((sessionId: string) => {
      if (sessionId === "task-chat:A") return new Promise((resolve) => { resolveA = resolve; });
      return Promise.resolve(idle([row("b", "user", "bee", "2026-11-25T00:00:01Z")]));
    });
    await show(<TaskWorkConversation taskId="A" />);
    await show(<TaskWorkConversation taskId="B" />);
    await flush();
    resolveA(idle([row("a", "user", "secret-from-a", "2026-11-25T00:00:00Z")]));
    await flush();
    expect(container.textContent).toContain("bee");
    expect(container.textContent).not.toContain("secret-from-a");
    expect(container.querySelector("[data-identity]")?.getAttribute("data-identity")).toBe("task:B");
  });

  it.each([
    [true, true, "task:t1"],
    [true, false, "planner:t1"],
    [false, true, "planner:t1"],
    [false, false, "planner:t1"],
  ])("selects the work conversation for enabled=%s and manager_answers=%s", async (enabled, answers, identity) => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url);
      if (init?.method === "GET") {
        return new Response(JSON.stringify({ status: "error", error: { code: "invalid", message: "use POST" } }), { status: 405 });
      }
      let result: unknown = {};
      if (path.includes("task_conversations")) result = { enabled, manager_answers: answers };
      else if (path.includes("tasks/detail")) result = { ...taskOf("t1"), requirement: "ship", name: "Beta", title: "Beta" };
      else if (path.includes("work_list")) result = { items: [], schedules: [], next_cursor: null };
      else if (path.includes("tasks/activity")) result = { activity: [] };
      else if (path.includes("tasks/mark_seen")) result = {};
      return new Response(JSON.stringify({ status: "ok", result }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    api.fetchChatTargets.mockResolvedValue([{ kind: "planner", id: "planner-t1", can_send: true, task_id: "t1", status: "running" }]);
    await show(<WorkPage sub="task/t1" onNavigate={() => {}} />);
    for (let attempt = 0; attempt < 15 && container.querySelector("[data-identity]")?.getAttribute("data-identity") !== identity; attempt += 1) {
      await flush();
    }
    expect(container.querySelector("[data-identity]")?.getAttribute("data-identity")).toBe(identity);
    if (identity === "task:t1") {
      expect(api.fetchChatHistory).toHaveBeenCalledWith("task-chat:t1", expect.anything());
      expect(api.fetchChatMessages).not.toHaveBeenCalled();
    } else {
      expect(api.fetchChatMessages).toHaveBeenCalledWith("planner-t1", expect.anything());
      expect(api.fetchChatHistory).not.toHaveBeenCalled();
    }
  });

  it("keeps the Planner when the conversation switch cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url);
      if (path.includes("task_conversations")) {
        return new Response(JSON.stringify({ status: "error", error: { code: "unavailable", message: "down" } }), { status: 500 });
      }
      if (init?.method === "GET") {
        return new Response(JSON.stringify({ status: "error", error: { code: "invalid", message: "use POST" } }), { status: 405 });
      }
      let result: unknown = {};
      if (path.includes("tasks/detail")) result = { ...taskOf("t1"), requirement: "ship", name: "Beta", title: "Beta" };
      else if (path.includes("work_list")) result = { items: [], schedules: [], next_cursor: null };
      else if (path.includes("tasks/activity")) result = { activity: [] };
      return new Response(JSON.stringify({ status: "ok", result }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    api.fetchChatTargets.mockResolvedValue([{ kind: "planner", id: "planner-t1", can_send: true, task_id: "t1", status: "running" }]);
    await show(<WorkPage sub="task/t1" onNavigate={() => {}} />);
    for (let attempt = 0; attempt < 15 && !container.querySelector("[data-identity]"); attempt += 1) await flush();
    expect(container.querySelector("[data-identity]")?.getAttribute("data-identity")).toBe("planner:t1");
    expect(api.fetchChatHistory).not.toHaveBeenCalled();
    expect(api.fetchChatMessages).toHaveBeenCalledWith("planner-t1", expect.anything());
  });

  it("sends, streams, stops and reloads one tool over the chat HTTP client", async () => {
    const posts: { url: string; body: Record<string, unknown> }[] = [];
    let reply: ReadableStreamDefaultController<Uint8Array> | undefined;
    let saved = false;
    const encoder = new TextEncoder();
    const tool = { tool_use_id: "tu1", name: "bash", args: "{}", result: "ok", is_error: false };
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
      posts.push({ url: String(url), body });
      const path = String(url);
      if (path.endsWith("/messages")) {
        return json(saved ? {
          messages: [
            row("user-1", "user", "ship it", "2026-11-25T00:00:00Z"),
            row("user-1:reply", "assistant", "stopped text", "2026-11-25T00:00:01Z"),
            row("user-1:tool:tu1", "tool", JSON.stringify(tool), "2026-11-25T00:00:02Z"),
          ],
          has_more: false, next_before: null, turn_state: "idle",
        } : { messages: [], has_more: false, next_before: null, turn_state: "idle" });
      }
      if (path.endsWith("/send")) return json({ correlation_id: "corr-1" });
      if (path.endsWith("/stop")) return json({ state: "stopping", phase: null, method: "control" });
      if (path.endsWith("/reply_events")) {
        return new Response(new ReadableStream({ start(controller) { reply = controller; } }), {
          status: 200, headers: { "Content-Type": "text/event-stream" },
        });
      }
      if (path.endsWith("/thread_events")) {
        return new Response(new ReadableStream({ start() {} }), {
          status: 200, headers: { "Content-Type": "text/event-stream" },
        });
      }
      return json({});
    }));
    const real = bridge.real!;
    api.fetchChatHistory.mockImplementation((...args: Parameters<typeof real.fetchChatHistory>) => real.fetchChatHistory(...args));
    api.sendChat.mockImplementation((...args: Parameters<typeof real.sendChat>) => real.sendChat(...args));
    api.streamChatReply.mockImplementation((...args: Parameters<typeof real.streamChatReply>) => real.streamChatReply(...args));
    api.streamChatThread.mockImplementation((...args: Parameters<typeof real.streamChatThread>) => real.streamChatThread(...args));
    api.stopChatReply.mockImplementation((...args: Parameters<typeof real.stopChatReply>) => real.stopChatReply(...args));

    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    expect(posts.find((post) => post.url.endsWith("/messages"))?.body).toEqual({ session_id: "task-chat:t1" });
    expect(posts.find((post) => post.url.endsWith("/thread_events"))?.body).toEqual({ session_id: "task-chat:t1" });
    await say("ship it");
    for (let attempt = 0; attempt < 8 && !reply; attempt += 1) await flush();
    const push = (chunk: string) => reply?.enqueue(encoder.encode(chunk));
    push('event: answer.delta\ndata: {"text":"working"}\n\n');
    push('event: agent.tool\ndata: {"tool_use_id":"tu1","name":"bash","args":"{}","result":"","is_error":false}\n\n');
    await flush();
    expect(container.querySelectorAll('[data-feed-key="tool:tu1"]')).toHaveLength(1);
    expect(container.querySelector('[data-feed-key="tool:tu1"]')?.textContent).toContain("进行中");
    push('event: agent.tool\ndata: {"tool_use_id":"tu1","name":"bash","args":"{}","result":"ok","is_error":false}\n\n');
    await flush();
    expect(container.querySelectorAll('[data-feed-key="tool:tu1"]')).toHaveLength(1);
    expect(container.querySelector('[data-feed-key="tool:tu1"]')?.textContent).toContain("完成");
    act(() => { (stopButton() as HTMLButtonElement).click(); });
    await flush();
    expect(container.textContent).toContain("正在停止…");
    const sent = posts.find((post) => post.url.endsWith("/send"));
    const stopped = posts.find((post) => post.url.endsWith("/stop"));
    expect(sent?.body).toMatchObject({ text: "ship it", session_id: "task-chat:t1", target: { kind: "task", task_id: "t1" } });
    expect(stopped?.body).toEqual({ message_id: sent?.body.message_id });
    push('event: answer.final\ndata: {"text":"stopped text","outcome":"stopped"}\n\n');
    reply?.close();
    await flush();
    expect(container.textContent).toContain("已停止");
    expect(container.textContent).not.toContain("正在停止…");

    saved = true;
    posts.length = 0;
    act(() => root.unmount());
    root = createRoot(container);
    await show(<TaskWorkConversation taskId="t1" task={taskOf("t1")} />);
    await flush();
    expect(container.querySelectorAll('[data-feed-key="tool:tu1"]')).toHaveLength(1);
    expect(container.querySelector('[data-feed-key="tool:user-1:tool:tu1"]')).toBeNull();
    expect(container.querySelector('[data-feed-key="tool:tu1"]')?.textContent).toContain("完成");
  });
});

function json(result: unknown): Response {
  return new Response(JSON.stringify({ status: "ok", result }), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
}
