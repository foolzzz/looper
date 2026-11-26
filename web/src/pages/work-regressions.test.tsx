// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { WorkPage } from "./work";

vi.mock("@/lib/use-projects", () => ({ useProjects: () => ({ projects: [{ project_id: "p", name: "Project" }] }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const row = (id: string) => ({ task_id: id, title: id, project_id: "p", project_name: "Project", status: "running", group: "in_progress", last_seq: 4 });
const calls: { path: string; body: Record<string, unknown> }[] = [];
let host: HTMLDivElement, root: Root;
let detailStatus = 200;
let delay: (() => Promise<void>) | null;
const releases: (() => void)[] = [];
let held = false;
async function settle() { for (let i = 0; i < 12; i++) await act(async () => { await Promise.resolve(); }); }
function Harness({ initial = "" }: { initial?: string }) {
  const [sub, setSub] = useState(initial);
  return <WorkPage sub={sub} onNavigate={setSub} />;
}
async function show(initial = "") { await act(async () => { root.render(<Harness initial={initial} />); }); await settle(); }
async function click(name: string) {
  const el = [...host.querySelectorAll("button")].find(el => el.textContent?.includes(name))!;
  expect(el).toBeTruthy();
  await act(async () => { el.click(); }); await settle();
}
async function remount(initial = "") { act(() => root.unmount()); root = createRoot(host); await show(initial); }
beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
  calls.length = 0; detailStatus = 200; delay = null; held = false; releases.length = 0;
  localStorage.clear(); sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}")); calls.push({ path: String(path), body });
    if (held && path.includes("work_list")) await new Promise<void>(resolve => releases.push(resolve));
    if (delay && path.includes("tasks/detail")) await delay();
    let result: unknown = {};
    if (path.includes("work_list")) result = body.section === "schedules"
      ? { schedules: [{ schedule_id: "s", name: "Nightly", consecutive_failures: 0 }], next_cursor: null }
      : { items: [row(body.cursor ? "later" : body.section)], next_cursor: body.cursor ? null : "second" };
    if (path.includes("tasks/page")) result = { tasks: Array.from({ length: body.before ? 2 : 10 }, (_, i) => ({ task_id: `run-${i + (body.before ? 10 : 0)}`, name: `Run ${i + (body.before ? 10 : 0)}`, status: "completed" })), next_before: body.before ? null : "older" };
    if (path.includes("tasks/detail")) result = { ...row(String(body.task_id)), title: "Outside first screen" };
    if (path.includes("tasks/activity")) result = { activity: [{ seq: 1501 }], has_more: true };
    if (path.includes("create_draft")) result = { task_id: "new", project_id: "p" };
    const status = init?.method === "GET" ? 405 : path.includes("tasks/detail") ? detailStatus : 200;
    return new Response(JSON.stringify(status === 200 ? { status: "ok", result } : { status: "error", error: { code: "not_found", message: "read failed" } }), { status });
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("reaches the eleventh scheduled run through the server cursor", async () => {
  await show(); await click("Nightly");
  const schedules = host.querySelector('[aria-label="定时任务"]')!;
  const more = [...schedules.querySelectorAll("button")].find(el => el.textContent === "加载更多")!;
  expect(more).toBeTruthy(); await act(async () => { more.click(); }); await settle();
  expect(schedules.querySelector('a[href="#work/task/run-11"]')).not.toBeNull();
  expect(calls.find(call => call.path.includes("tasks/page") && call.body.before === "older")?.body).toEqual({ schedule_id: "s", limit: 10, before: "older" });
  await remount();
  expect(host.querySelector('[aria-label="定时任务"] a[href="#work/task/run-11"]')).not.toBeNull();
});

it("reads deep-linked and reloaded tasks independently with the latest watermark", async () => {
  await show("task/outside");
  expect(host.querySelector('[aria-label="当前任务"] h1')?.textContent).toBe("Outside first screen");
  expect(host.querySelector('a[href="#chat?target=planner-outside"]')).toBeNull();
  expect(host.querySelector('textarea[aria-label="消息"]')).not.toBeNull();
  expect(calls.filter(c => c.path.includes("mark_seen")).map(c => c.body)).toEqual([{ task_id: "outside", seq: 1501 }]);
  await remount("task/outside");
  expect(host.querySelector('[aria-label="当前任务"] h1')?.textContent).toBe("Outside first screen");
});

it("distinguishes selected task loading, missing and error states without marking seen", async () => {
  let release!: () => void; delay = () => new Promise<void>(resolve => { release = resolve; });
  await show("task/outside");
  expect(host.querySelector('[aria-label="当前任务"]')?.textContent).toContain("正在加载");
  detailStatus = 404; delay = null; await act(async () => { release(); }); await settle();
  expect(host.querySelector('[aria-label="当前任务"]')?.textContent).toContain("任务不存在");
  detailStatus = 500; await remount("task/outside");
  expect(host.querySelector('[aria-label="当前任务"] [role="alert"]')).not.toBeNull();
  expect(calls.some(c => c.path.includes("mark_seen"))).toBe(false);
});

it("rebuilds loaded pages and expanded schedule runs before restoring delayed content", async () => {
  await show(); await click("Nightly"); await click("已结束");
  const ended = host.querySelector('[aria-label="已结束"]')!;
  await act(async () => { [...ended.querySelectorAll("button")].find(el => el.textContent === "加载更多")!.click(); }); await settle();
  const list = host.querySelector('[aria-label="工作台任务"]') as HTMLElement;
  list.scrollTop = 600; await act(async () => { list.dispatchEvent(new Event("scroll")); });
  held = true; await remount();
  const reloaded = host.querySelector('[aria-label="工作台任务"]') as HTMLElement;
  expect(reloaded.scrollTop).toBe(0);
  expect(sessionStorage.getItem("looper.work.scroll")).toBe("600");
  held = false; await act(async () => { releases.splice(0).forEach(resolve => resolve()); }); await settle();
  expect(host.querySelector('[aria-label="已结束"] a[href="#work/task/later"]')).not.toBeNull();
  expect(host.querySelector('[aria-label="定时任务"] a[href="#work/task/run-9"]')).not.toBeNull();
  expect(reloaded.scrollTop).toBe(600);
});

it("returns search focus on Escape without reopening, leaving Manager and task links reachable", async () => {
  await show(); const search = host.querySelector('input[aria-label="查找任务"]') as HTMLInputElement;
  await act(async () => { search.focus(); search.click(); }); await settle();
  expect(host.querySelector('[role="dialog"]')).not.toBeNull();
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1)); });
  expect(document.activeElement).not.toBe(search);
  await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); }); await settle();
  expect(document.activeElement).toBe(search); expect(host.querySelector('[role="dialog"]')).toBeNull();
  const manager = host.querySelector('a[href="#work/manager"]') as HTMLElement;
  manager.focus(); expect(document.activeElement).toBe(manager);
  const task = host.querySelector('a[href="#work/task/open"]') as HTMLElement;
  task.focus(); expect(document.activeElement).toBe(task);
});

it("creates a draft with N only outside editable controls", async () => {
  await show(); const search = host.querySelector("input")!;
  await act(async () => { search.focus(); }); await act(async () => { search.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true })); }); await settle();
  expect(calls.filter(c => c.path.includes("create_draft") && Object.keys(c.body).length)).toHaveLength(0);
  search.blur(); await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "n", bubbles: true })); }); await settle();
  expect(calls.filter(c => c.path.includes("create_draft") && c.body.project_id === "p")).toHaveLength(1);
});

it("waits for a hidden narrow-screen list to have layout before restoring", async () => {
  const height = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(0);
  let resized!: () => void;
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resized = callback; }
    observe() {}
    disconnect() {}
  });
  sessionStorage.setItem("looper.work.scroll", "600");
  await show("task/outside");
  const list = host.querySelector('[aria-label="工作台任务"]') as HTMLElement;
  expect(list.scrollTop).toBe(0);
  height.mockReturnValue(600);
  await act(async () => { resized(); });
  expect(list.scrollTop).toBe(600);
});
