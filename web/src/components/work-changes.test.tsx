// @vitest-environment happy-dom
import { act, useLayoutEffect, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkChanges } from "./work-changes";
import { WorkPage } from "@/pages/work";

vi.mock("@/lib/use-projects", () => ({ useProjects: () => ({ projects: [] }) }));
vi.mock("@/components/work-list", () => ({ WorkList: () => <div>任务列表</div> }));
vi.mock("@/components/work-conversation", async importOriginal => ({
  ...await importOriginal<typeof import("@/components/work-conversation")>(),
  PlannerWorkConversation: () => <textarea aria-label="Message" />,
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ok = (taskId = "a", files: object[] = [], extra: object = {}) => ({
  task_id: taskId, kind: "ok", scope: "task_branch", files, truncated: false, reason: null, ...extra,
});
const file = (path = "new.ts", status = "modified", old_path: string | null = null) => ({ path, status, old_path });
const answer = (result: object) => Response.json({ status: "ok", result });
const failure = (status: number, message: string) => Response.json({ status: "error", error: { code: status === 404 ? "not_found" : "forbidden", message } }, { status });
let host: HTMLDivElement, root: Root;
let taskStatus: string;
let respond: (body: { task_id: string; limit: number }, signal: AbortSignal) => Response | Promise<Response>;
const calls: { path: string; body: Record<string, unknown>; signal?: AbortSignal | null }[] = [];
async function settle() { for (let i = 0; i < 8; i++) await act(async () => { await Promise.resolve(); }); }
async function show(node: ReactNode = <WorkChanges taskId="a" visible />) {
  await act(async () => { root.render(node); }); await settle();
}
async function click(text: string) {
  const button = [...host.querySelectorAll("button")].find(el => el.textContent === text)!;
  expect(button).toBeTruthy();
  await act(async () => { button.click(); }); await settle();
}

beforeEach(() => {
  calls.length = 0;
  taskStatus = "running";
  respond = body => answer(ok(body.task_id));
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    calls.push({ path: String(path), body, signal: init?.signal });
    if (path === "/api/v1/tasks/changes") return respond(body, init!.signal!);
    if (path === "/api/v1/tasks/detail") return answer({ task_id: body.task_id, title: "Selected task", status: taskStatus });
    if (path === "/api/v1/tasks/activity") return answer({ activity: [] });
    return answer({});
  }));
  localStorage.clear(); sessionStorage.clear();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); });

it("reads once on opening with the closed request, distinguishing loading and a clean workspace", async () => {
  let release!: (response: Response) => void;
  respond = () => new Promise(resolve => { release = resolve; });
  await show(<WorkChanges taskId="a" visible={false} />);
  expect(calls).toHaveLength(0); expect(host.textContent).toBe("");
  await show();
  expect(host.textContent).toContain("正在读取改动…");
  expect(host.textContent).not.toContain("没有改动文件");
  expect(calls.map(c => ({ path: c.path, body: c.body }))).toEqual([
    { path: "/api/v1/tasks/changes", body: { task_id: "a", limit: 100 } },
  ]);
  await act(async () => { release(answer(ok())); }); await settle();
  expect(host.textContent).toContain("没有改动文件");
  expect(host.textContent).toContain("任务分支改动");
  expect(host.textContent).not.toContain("共0个");
});

it.each(["task_branch", "working_tree"])("shows %s, literal rename paths and truncation without content requests", async scope => {
  const old = '<img src=x onerror="alert(1)">/old.ts';
  const path = `目录/${"long/".repeat(80)}<script>bad()</script>.ts`;
  respond = () => answer(ok("a", [file(path, "renamed", old)], { scope, truncated: true }));
  await show();
  expect(host.textContent).toContain(scope === "task_branch" ? "任务分支改动" : "当前未提交改动（目录可能由多个任务共用）");
  expect(host.textContent).toContain(`${old} → ${path}`);
  expect(host.textContent).toContain("重命名");
  expect(host.textContent).toContain("仅显示前 1 个文件");
  expect(host.querySelectorAll("script, img, a")).toHaveLength(0);
  expect(host.querySelector("li")?.className).toContain("[overflow-wrap:anywhere]");
  expect(host.textContent).not.toContain("共1个");
  expect(calls).toHaveLength(1);
  expect([...host.querySelectorAll("button")].map(el => el.textContent)).toEqual(["刷新"]);
});

it.each([
  ["added", "新增"], ["modified", "修改"], ["deleted", "删除"], ["renamed", "重命名"],
  ["copied", "复制"], ["type_changed", "类型变更"], ["conflicted", "冲突"], ["untracked", "未跟踪"], ["unknown", "状态未知"],
])("labels the %s file status", async (status, copy) => {
  respond = () => answer(ok("a", [file("file.ts", status)]));
  await show(); expect(host.querySelector("li")?.textContent).toContain(copy);
});

it("explains an unstarted task without a workspace before making a changes request", async () => {
  await show(<WorkChanges taskId="a" visible pendingWorkspace />);
  expect(host.textContent).toContain("任务开始后显示改动");
  expect(host.querySelector('[role="alert"], .text-destructive')).toBeNull();
  expect(calls).toHaveLength(0);
  await show();
  expect(calls).toHaveLength(1);
  expect(host.textContent).toContain("没有改动文件");
});

it.each(["draft", "init"])("keeps the %s right panel neutral before the task starts", async status => {
  taskStatus = status;
  respond = () => answer(ok("a", [], { kind: "unavailable", scope: null, reason: "workspace_missing" }));
  await show(<WorkPage sub="task/a" onNavigate={vi.fn()} />);
  await click("任务详情");
  const changes = host.querySelector('[aria-label="改动文件"]')!;
  expect(changes.textContent).toContain("任务开始后显示改动");
  expect(changes.querySelector("button, [role=alert], .text-destructive")).toBeNull();
  expect(calls.filter(call => call.path.endsWith("/changes"))).toHaveLength(0);
  expect(host.querySelector("#work-steps")?.textContent).toContain("尚无步骤");
  expect(host.querySelector('[aria-label="关卡"]')?.textContent).toContain("没有关卡");
  expect(host.querySelector('[aria-labelledby="work-cost-title"]')?.textContent).toContain("费用尚不可用");
});

it.each(["pending", "assigned"])("keeps %s workspace_missing neutral until a workspace exists", async status => {
  taskStatus = status;
  respond = () => answer(ok("a", [], { kind: "unavailable", scope: null, reason: "workspace_missing" }));
  await show(<WorkPage sub="task/a" onNavigate={vi.fn()} />);
  await click("任务详情");
  const changes = host.querySelector('[aria-label="改动文件"]')!;
  expect(changes.textContent).toContain("工作目录就绪后显示改动");
  expect(changes.textContent).not.toContain("无法读取改动");
  expect(changes.querySelector("button, [role=alert], .text-destructive")).toBeNull();
  expect(calls.filter(call => call.path.endsWith("/changes"))).toHaveLength(0);
});

it.each(["running", "planning", "executing", "verifying", "stopping", "waiting", "waiting_user", "awaiting_budget", "handing_back", "completed", "failed", "cancelled", "stopped"])(
  "keeps real changes failures distinct from the other empty %s panel blocks", async status => {
    taskStatus = status;
    respond = () => answer(ok("a", [], { kind: "unavailable", scope: null, reason: "workspace_missing" }));
    await show(<WorkPage sub="task/a" onNavigate={vi.fn()} />);
    await click("任务详情");
    const changes = host.querySelector('[aria-label="改动文件"]')!;
    expect(changes.querySelector('[role="alert"]')?.textContent).toContain("无法读取改动：工作目录不存在");
    expect(changes.querySelector("button")?.textContent).toBe("重试");
    expect(changes.textContent).not.toContain("工作目录就绪后显示改动");
    expect(changes.textContent).not.toContain("任务开始后显示改动");
    expect(calls.filter(call => call.path.endsWith("/changes"))).toHaveLength(1);
    for (const selector of ["#work-steps", '[aria-label="关卡"]', '[aria-labelledby="work-cost-title"]']) {
      const block = host.querySelector(selector)!;
      expect(block.querySelector('[role="alert"], .text-destructive')).toBeNull();
      expect(block.textContent).toMatch(/尚无步骤|没有关卡|费用尚不可用/);
    }
  },
);

it("keeps a real non-Git directory neutral without calling it a read failure", async () => {
  respond = () => answer(ok("a", [], { kind: "unavailable", scope: null, reason: "not_git" }));
  await show();
  expect(host.textContent).toContain("工作目录不是 Git 仓库");
  expect(host.textContent).not.toContain("无法读取改动");
  expect(host.querySelector('[role="alert"], .text-destructive')).toBeNull();
  expect(calls).toHaveLength(1);
  respond = () => answer(ok());
  await click("刷新");
  expect(host.textContent).toContain("没有改动文件");
});

it.each([
  ["workspace_missing", "工作目录不存在"],
  ["branch_missing", "任务分支不存在"], ["branch_changed", "工作区分支已变更"], ["git_error", "Git 读取失败"],
])("explains unavailable %s and retries exactly once", async (reason, copy) => {
  respond = () => answer(ok("a", [], { kind: "unavailable", scope: null, reason }));
  await show();
  expect(host.textContent).toContain(`无法读取改动：${copy}`);
  expect(host.querySelector('[role="alert"]')?.className).toContain("text-destructive");
  expect(host.textContent).not.toContain("没有改动文件");
  respond = () => answer(ok());
  await click("重试");
  expect(calls).toHaveLength(2); expect(host.textContent).toContain("没有改动文件");
});

it.each([
  [404, "no POST /api/v1/tasks/changes", "此服务器尚不支持改动文件"],
  [404, "Task a was not found", "任务不存在"],
  [403, "Operator access required", "Operator access required"],
  [0, "Network offline", "Network offline"],
])("keeps HTTP/network error %s distinct from an empty success", async (status, message, copy) => {
  respond = () => { if (!status) throw new Error(message); return failure(status, message); };
  await show();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain(copy);
  expect(host.textContent).not.toContain("没有改动文件");
  expect(host.textContent).toContain("重试");
});

it("rejects a pending confirmation rather than claiming a clean workspace", async () => {
  respond = () => Response.json({ status: "needs_confirmation", impact: "Unexpected confirmation" }, { status: 202 });
  await show(); expect(host.textContent).toContain("Unexpected confirmation");
  expect(host.textContent).not.toContain("没有改动文件");
});

it.each([false, true])("discards A's late %s response after switching to B, even if fetch ignores abort", async rejects => {
  let release!: () => void;
  respond = body => body.task_id === "a" ? new Promise((resolve, reject) => {
    release = () => rejects ? reject(new Error("A failed")) : resolve(answer(ok("a", [file("A-only.ts")])));
  }) : answer(ok("b", [file("B-only.ts")]));
  await show(); const signal = calls[0].signal!;
  await show(<WorkChanges taskId="b" visible />);
  expect(signal.aborted).toBe(true);
  await act(async () => { release(); }); await settle();
  expect(host.textContent).toContain("B-only.ts");
  expect(host.textContent).not.toContain("A-only.ts"); expect(host.textContent).not.toContain("A failed");
});

it("never renders cached A files during B's initial render", async () => {
  const paints: string[] = [];
  function Probe({ taskId }: { taskId: string }) {
    useLayoutEffect(() => { paints.push(host.textContent!); }, [taskId]);
    return <WorkChanges taskId={taskId} visible />;
  }
  respond = body => body.task_id === "a" ? answer(ok("a", [file("A-only.ts")])) : new Promise(() => {});
  await show(<Probe taskId="a" />); expect(host.textContent).toContain("A-only.ts");
  await show(<Probe taskId="b" />);
  expect(paints.at(-1)).not.toContain("A-only.ts");
  expect(host.textContent).toContain("正在读取改动…"); expect(host.textContent).not.toContain("A-only.ts");
});

it("aborts on closing, never polls while hidden, and re-reads on reopening", async () => {
  vi.useFakeTimers();
  let release!: (response: Response) => void;
  respond = () => new Promise(resolve => { release = resolve; });
  await show(); const signal = calls[0].signal!;
  await show(<WorkChanges taskId="a" visible={false} />);
  expect(signal.aborted).toBe(true);
  await act(async () => { release(answer(ok("a", [file("late.ts")]))); await vi.advanceTimersByTimeAsync(60_000); });
  expect(host.textContent).toBe(""); expect(calls).toHaveLength(1);
  respond = () => answer(ok()); await show();
  expect(calls).toHaveLength(2); expect(host.textContent).toContain("没有改动文件");
});

it("does not poll an open panel or retain a request after unmount", async () => {
  vi.useFakeTimers();
  respond = () => new Promise(() => {});
  await show();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(calls).toHaveLength(1);
  await show(<div />);
  expect(calls[0].signal?.aborted).toBe(true);
});

it("labels retained files during refresh and refresh failure, then replaces them on retry", async () => {
  respond = () => answer(ok("a", [file("cached.ts")])); await show();
  let reject!: (error: Error) => void;
  respond = () => new Promise((_, no) => { reject = no; }); await click("刷新");
  expect(host.textContent).toContain("cached.ts"); expect(host.textContent).toContain("正在刷新");
  await act(async () => { reject(new Error("Offline")); }); await settle();
  expect(host.textContent).toContain("刷新失败"); expect(host.textContent).toContain("Offline");
  expect(host.textContent).toContain("cached.ts"); expect(host.textContent).not.toContain("没有改动文件");
  respond = () => answer(ok()); await click("重试");
  expect(calls).toHaveLength(3); expect(host.textContent).not.toContain("cached.ts");
  expect(host.textContent).toContain("没有改动文件");
});

it("keeps the selected task and composer usable when the files section fails, with close/Escape focus return", async () => {
  respond = () => failure(403, "Files forbidden");
  await show(<WorkPage sub="task/a" onNavigate={vi.fn()} />);
  expect(calls.filter(c => c.path.endsWith("/changes"))).toHaveLength(0);
  const opener = [...host.querySelectorAll("button")].find(el => el.textContent === "任务详情")!;
  await click("任务详情");
  expect(host.textContent).toContain("Files forbidden");
  expect(host.querySelector("h1")?.textContent).toBe("Selected task");
  expect(host.querySelector('textarea[aria-label="Message"]')).not.toBeNull();
  expect(host.querySelector('[aria-label="任务详情"]')?.contains(document.activeElement)).toBe(true);
  await act(async () => { document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); }); await settle();
  expect(document.activeElement).toBe(opener);
  expect(host.querySelector('[aria-label="改动文件"]')).toBeNull();
  await click("任务详情"); await click("关闭任务详情");
  expect(document.activeElement).toBe(opener);
  expect(calls.filter(c => c.path.endsWith("/changes"))).toHaveLength(2);
});
