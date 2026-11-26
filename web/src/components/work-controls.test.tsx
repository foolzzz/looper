// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkControls } from "./work-controls";
import type { TaskDetail } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const task = (status = "init", extra: Partial<TaskDetail> = {}) => ({
  task_id: "a", status, group: "ready", requirement: "Build a fixture", work_dir: "workspace",
  attempt_ended_at: null, error: null, ...extra,
}) as TaskDetail;
let host: HTMLDivElement, root: Root;
let reads: TaskDetail[], readError: Error | undefined;
let signals: AbortSignal[], writes: string[], replyStops: number;
let writeResponse: () => Promise<Response>;
const ok = (result: unknown) => Response.json({ status: "ok", result });
const refresh = vi.fn(async (signal?: AbortSignal) => {
  if (signal) signals.push(signal);
  if (readError) throw readError;
  return reads.length > 1 ? reads.shift()! : reads[0];
});
function Harness({ initial }: { initial: TaskDetail }) {
  const [current, setCurrent] = useState(initial);
  return <><textarea aria-label="未发消息" defaultValue="keep my draft" /><WorkControls task={current} onRefresh={async signal => {
    const next = await refresh(signal);
    if (next && !signal?.aborted) setCurrent(next);
    return next;
  }} /></>;
}
async function settle() { await act(async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); }); }
async function show(value = task()) { await act(async () => root.render(<Harness key={value.task_id} initial={value} />)); await settle(); }
const button = (name: string) => [...host.querySelectorAll("button")].find(el => el.textContent === name)!;
async function click(name: string, twice = false) {
  act(() => { button(name).click(); if (twice) button(name).click(); }); await settle();
}
async function tick() { await act(async () => { await vi.advanceTimersByTimeAsync(2000); }); await settle(); }
beforeEach(() => {
  vi.useFakeTimers(); resetSessionGuard(); reads = [task()]; readError = undefined;
  signals = []; writes = []; replyStops = 0; refresh.mockClear();
  writeResponse = async () => ok({ task_id: "a", started: true, restarted: true, stopped: true });
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (path.includes("stop_reply")) replyStops++;
    writes.push(path); return writeResponse();
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); resetSessionGuard(); });

it.each([
  ["init", "开始", "start"], ["draft", "开始", "start"],
  ["failed", "继续后台任务", "restart"], ["stopped", "继续后台任务", "restart"],
  ["cancelled", "继续后台任务", "restart"], ["running", "停止后台任务", "stop"],
])("%s uses its registered operation once for synchronous double clicks", async (status, label, operation) => {
  reads = [task(status)]; await show(task(status)); await click(label, true);
  expect(writes).toEqual([`/api/v1/ops/task_control/${operation}`]);
  expect(replyStops).toBe(0);
  expect(host.textContent).toContain("操作尚未确认");
  expect(host.querySelector("textarea")?.value).toBe("keep my draft");
});

it.each([["", "workspace", "请先填写需求"], ["need", "", "请先选择有效目录"]])("draft validates requirement and directory", async (requirement, work_dir, reason) => {
  await show(task("draft", { requirement, work_dir }));
  expect(button("开始").disabled).toBe(true); expect(host.textContent).toContain(reason);
  await click("开始"); expect(writes).toEqual([]);
});

it("locks both baseline reads and writes while a double click waits for its first refresh", async () => {
  let release!: (value: TaskDetail) => void;
  refresh.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  await show();
  await click("开始", true);
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(writes).toEqual([]);
  reads = [task("running")];
  await act(async () => release(task())); await settle();
  expect(writes).toEqual(["/api/v1/ops/task_control/start"]);
  expect(host.textContent).toContain("操作已确认");
});

it("a rejected draft start explains the unavailable draft entry point", async () => {
  reads = [task("draft")]; await show(task("draft"));
  writeResponse = async () => Response.json({ status: "error", error: { code: "conflict", message: "task is not init" } }, { status: 409 });
  await click("开始");
  expect(host.querySelector("[role='alert']")?.textContent).toContain("此服务器尚未提供草稿开始入口");
  expect(host.textContent).toContain("task is not init");
  expect(host.textContent).not.toContain("先停止这一轮");
  expect(writes).toEqual(["/api/v1/ops/task_control/start"]);
});

it("keeps a 4xx refusal visible after rereading until the next action", async () => {
  writeResponse = async () => Response.json({ status: "error", error: { code: "invalid_request", message: "specific refusal" } }, { status: 400 });
  await show(); await click("开始");
  expect(button("开始").disabled).toBe(true);
  await click("重新读取");
  expect(button("开始").disabled).toBe(false);
  expect(host.querySelector("[role='alert']")?.textContent).toContain("specific refusal");
  expect(writes).toHaveLength(1);
  let release!: (response: Response) => void;
  writeResponse = () => new Promise(resolve => { release = resolve; });
  await click("开始");
  expect(host.textContent).not.toContain("specific refusal");
  await act(async () => release(ok({ task_id: "a", started: true }))); await settle();
});

it("a missing draft API never uses restart", async () => {
  reads = [task("draft")]; await show(task("draft"));
  writeResponse = async () => Response.json({ status: "error", error: { code: "not_found", message: "no operation" } }, { status: 404 });
  await click("开始");
  expect(host.textContent).toContain("此服务器尚未提供草稿开始入口");
  expect(writes).toEqual(["/api/v1/ops/task_control/start"]);
});

it.each(["completed", "mystery"])("%s has no background restart shortcut", async status => {
  await show(task(status)); expect(button("开始")).toBeUndefined(); expect(button("继续后台任务")).toBeUndefined();
  expect(button("停止后台任务")).toBeUndefined(); expect(writes).toEqual([]);
});

it("requesting and accepted are separate; running confirms immediately", async () => {
  let release!: (response: Response) => void;
  writeResponse = () => new Promise(resolve => { release = resolve; });
  reads = [task(), task("running")]; await show(); await click("开始");
  expect(host.textContent).toContain("正在请求"); expect(refresh).toHaveBeenCalledTimes(1);
  await act(async () => release(ok({ task_id: "a", started: true }))); await settle();
  expect(host.textContent).toContain("操作已确认");
  expect(host.textContent).not.toContain("操作尚未确认");
});

it("rereads before start and never confirms an old terminal or new aggregate counts alone", async () => {
  const before = task("stopped", { attempt_ended_at: "old" });
  reads = [before, { ...before, cost: { runs: 99, running: 0 } } as TaskDetail];
  await show(task("failed")); await click("继续后台任务"); await tick(); await tick();
  expect(host.textContent).toContain("操作尚未确认");
  expect(button("继续后台任务").disabled).toBe(true);
  reads = [task("running")]; await tick(); expect(host.textContent).toContain("操作已确认");
});

it.each(["completed", "failed", "stopped", "cancelled"])("fast %s needs the same new terminal snapshot twice", async status => {
  const before = task("failed", { attempt_ended_at: "old" });
  const ended = task(status, { attempt_ended_at: "new", error: status === "failed" ? "specific failure" : null });
  reads = [before, ended]; await show(before); await click("继续后台任务");
  expect(host.textContent).toContain("操作尚未确认");
  await tick();
  expect(host.textContent).toContain("操作已确认");
  if (status === "failed") expect(host.textContent).toContain("specific failure");
});

it("a changed terminal with running audit rows still waits without a new end marker", async () => {
  reads = [task("stopped"), task("failed", { cost: { runs: 2, running: 1 } as TaskDetail["cost"] })];
  await show(task("stopped")); await click("继续后台任务"); await tick();
  expect(host.textContent).toContain("操作尚未确认");
  reads = [task("failed", { cost: { runs: 2, running: 0 } as TaskDetail["cost"] })];
  await tick(); expect(host.textContent).toContain("操作尚未确认");
  await tick(); expect(host.textContent).toContain("操作已确认");
});

it("stop waits for a terminal readback rather than stopping the reply", async () => {
  reads = [task("running"), task("stopping"), task("stopped")];
  await show(task("running")); await click("停止后台任务");
  expect(host.textContent).toContain("操作尚未确认"); await tick();
  expect(host.textContent).toContain("操作已确认"); expect(replyStops).toBe(0);
});

it.each(["task_manager_busy", "task_writer_busy"])("409 %s shows the upstream reason and stop-this-turn guidance", async reason => {
  writeResponse = async () => Response.json({ status: "error", error: { code: "conflict", message: `${reason}: holder is active` } }, { status: 409 });
  await show(); await click("开始");
  expect(host.textContent).toContain(`${reason}: holder is active`);
  expect(host.textContent).toContain("先停止这一轮"); expect(host.textContent).toContain("操作尚未确认");
  expect(host.textContent).not.toContain("操作已确认");
});

it.each(["network", "confirmation", "readback", "timeout"])("%s stays unconfirmed; retry only reads and preserves input", async failure => {
  if (failure === "network") writeResponse = async () => { throw new TypeError("network unavailable"); };
  if (failure === "confirmation") writeResponse = async () => Response.json({ status: "needs_confirmation", impact: "Please confirm first" }, { status: 202 });
  reads = [task(), task()]; await show(); await click("开始");
  if (failure === "readback") { readError = new Error("readback unavailable"); await tick(); }
  if (failure === "timeout") await act(async () => { await vi.advanceTimersByTimeAsync(32000); });
  expect(host.textContent).toContain("操作尚未确认"); expect(host.textContent).not.toContain("操作已确认");
  if (failure === "confirmation") expect(host.textContent).toContain("Please confirm first");
  readError = undefined; await click("重新读取"); expect(writes).toHaveLength(1);
  expect(host.querySelector("textarea")?.value).toBe("keep my draft");
});

it("switching task aborts readback and drops a late write result", async () => {
  let release!: (response: Response) => void;
  writeResponse = () => new Promise(resolve => { release = resolve; });
  await show(); await click("开始"); const signal = signals[0];
  await show(task("completed", { task_id: "b" }));
  expect(signal.aborted).toBe(true);
  await act(async () => release(ok({ task_id: "a", started: true }))); await settle();
  expect(host.textContent).not.toContain("操作尚未确认"); expect(refresh).toHaveBeenCalledTimes(1);
});

it("a late accepted read is aborted on a task switch and cannot settle the new panel", async () => {
  let release!: (value: TaskDetail) => void;
  refresh.mockImplementationOnce(async signal => { if (signal) signals.push(signal); return task(); });
  refresh.mockImplementationOnce(signal => {
    if (signal) signals.push(signal);
    return new Promise(resolve => { release = resolve; });
  });
  await show(); await click("开始");
  expect(host.textContent).toContain("服务器已接受"); const acceptedRead = signals.at(-1)!;
  await show(task("completed", { task_id: "b" })); expect(acceptedRead.aborted).toBe(true);
  await act(async () => release(task("running"))); await settle();
  expect(host.textContent).not.toContain("操作已确认"); expect(button("开始")).toBeUndefined();
});

it("an unknown readback state and changing terminal snapshots never confirm an attempt", async () => {
  const ended = task("completed", { attempt_ended_at: "new" });
  reads = [task(), task("mystery")]; await show(); await click("开始"); await tick();
  expect(host.textContent).toContain("操作尚未确认");
  reads = [ended, task("failed", { attempt_ended_at: "newer" }), ended];
  await tick(); await tick(); await tick(); expect(host.textContent).toContain("操作尚未确认");
  await tick(); expect(host.textContent).toContain("操作已确认");
});

it("a failed baseline read never sends a write", async () => {
  readError = new Error("baseline unavailable"); await show(); await click("开始");
  expect(host.textContent).toContain("baseline unavailable"); expect(writes).toEqual([]);
  readError = undefined; await click("重新读取"); expect(writes).toEqual([]);
});

it("done uses the dedicated operation and verifies persisted completion including warnings", async () => {
  writeResponse = async () => ok({ task_id: "a", status: "completed", changed: true, warning: "Workspace requires attention" });
  reads = [task(), task("completed")]; await show(); await click("完成", true);
  expect(writes).toEqual(["/api/v1/tasks/done"]); expect(host.textContent).toContain("操作已确认");
  expect(host.textContent).toContain("Workspace requires attention");
});

it("a missing done operation is explicitly unavailable", async () => {
  writeResponse = async () => Response.json({ status: "error", error: { code: "not_found", message: "no operation" } }, { status: 404 });
  await show(); await click("完成"); expect(host.textContent).toContain("完成暂不可用"); expect(button("完成").disabled).toBe(true);
});

it("pause, background and takeover are disabled with a concrete reason", async () => {
  await show();
  for (const label of ["暂停", "交给后台", "接手"]) {
    expect(button(label).disabled).toBe(true); expect(button(label).getAttribute("aria-describedby")).toBeTruthy();
    await click(label);
  }
  expect(host.textContent).toContain("此控制将在后续步骤提供"); expect(writes).toEqual([]);
});
