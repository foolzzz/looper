// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkList } from "@/components/work-list";
import { WorkPage } from "@/pages/work";
import { useProjects } from "@/lib/use-projects";
import { DRAFTS_UNAVAILABLE, FIND_UNAVAILABLE, type WorkSchedule, type WorkTask } from "@/lib/work";

vi.mock("@/lib/use-projects", () => ({ useProjects: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Answer { status: number; body: unknown }
type Handler = (url: string, method: string, body: Record<string, unknown>) => Answer;

const calls: { url: string; body: Record<string, unknown> }[] = [];
let handler: Handler = () => ({ status: 200, body: { status: "ok", result: {} } });
let host: HTMLDivElement;
let root: Root;

function task(partial: Partial<WorkTask> & Pick<WorkTask, "task_id">): WorkTask {
  return {
    title: partial.task_id, project_id: "p-beta", project_name: "Beta", status: "completed", group: "completed",
    last_activity_at: "2026-11-24T00:00:00Z", last_seq: 1, waiting_gates: 0, needs_you: false, unseen: false, ...partial,
  };
}

const OPEN: WorkTask[] = [
  task({ task_id: "t-b1", title: "Beta new", status: "running", group: "in_progress", unseen: true, last_seq: 7 }),
  task({
    task_id: "t-a1", title: "Alpha wait", project_id: "p-alpha", project_name: "Alpha",
    status: "waiting_user", group: "needs_you", needs_you: true, waiting_gates: 2,
  }),
  task({ task_id: "t-b2", title: "", status: "init", group: "ready" }),
];

const SCHEDULE: WorkSchedule = {
  schedule_id: "s1", name: "Nightly", project_id: "p-beta", project_name: "Beta", enabled: true,
  consecutive_failures: 3, last_success_at: null, last_task_id: "run-1", last_status: "failed",
  last_ended_at: "2026-11-24T01:00:00Z", running: true,
};

const ENDED: Record<string, { items: WorkTask[]; next_cursor: string | null }> = {
  "": { items: [task({ task_id: "e1" }), task({ task_id: "e2" })], next_cursor: "c1" },
  c1: { items: [task({ task_id: "e3" }), task({ task_id: "e4" })], next_cursor: "c2" },
  c2: { items: [task({ task_id: "e5" })], next_cursor: null },
};

let failSchedules = false;
let missingList = false;
let missingDraft = false;
let missingSelect = false;

function answer(url: string, method: string, body: Record<string, unknown>): Answer {
  if (method === "GET" && url.includes("create_draft") && missingDraft) {
    return { status: 404, body: { status: "error", error: { code: "not_found", message: `no GET ${url}` } } };
  }
  if (method === "GET" && url.includes("/select") && missingSelect) {
    return { status: 404, body: { status: "error", error: { code: "not_found", message: `no GET ${url}` } } };
  }
  if (method === "GET") return { status: 405, body: { status: "error", error: { code: "invalid", message: "use POST" } } };
  if (url.includes("tasks/work_list")) {
    if (missingList) return { status: 404, body: { status: "error", error: { code: "not_found", message: `no POST ${url}` } } };
    if (body.section === "schedules" && failSchedules) {
      return { status: 500, body: { status: "error", error: { code: "unavailable", message: "database unavailable" } } };
    }
    if (body.section === "open") return { status: 200, body: { status: "ok", result: { items: OPEN, next_cursor: null } } };
    if (body.section === "schedules") return { status: 200, body: { status: "ok", result: { schedules: [SCHEDULE], next_cursor: null } } };
    const page = ENDED[String(body.cursor ?? "")] ?? ENDED[""];
    return { status: 200, body: { status: "ok", result: page } };
  }
  if (url.includes("tasks/page")) {
    return { status: 200, body: { status: "ok", result: { tasks: [{ task_id: "run-1", name: "Nightly run", status: "failed" }], next_before: null, counts: {}, hidden_scheduled: 0 } } };
  }
  return { status: 200, body: { status: "ok", result: {} } };
}

async function settle() {
  for (let i = 0; i < 6; i += 1) await act(async () => { await Promise.resolve(); });
}
function button(rootNode: ParentNode, name: string): HTMLButtonElement {
  const buttons = [...rootNode.querySelectorAll("button")];
  const found = buttons.find((item) => item.textContent?.trim() === name)
    ?? buttons.find((item) => item.textContent?.includes(name));
  if (!found) throw new Error(`no button ${name}`);
  return found as HTMLButtonElement;
}
async function click(el: Element) {
  await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  await settle();
}
async function show() {
  await act(async () => {
    root.render(
      <WorkList selection={{ kind: "manager" }} preferredProjectId={null} onOpenTask={vi.fn()} onOpenManager={vi.fn()}
        onFinder={vi.fn()} onFind={vi.fn()} />,
    );
  });
  await settle();
}

beforeEach(() => {
  calls.length = 0;
  failSchedules = false;
  missingList = false;
  missingDraft = false;
  missingSelect = false;
  handler = answer;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    calls.push({ url: String(url), body });
    const next = handler(String(url), method, body);
    return new Response(JSON.stringify(next.body), { status: next.status, headers: { "Content-Type": "application/json" } });
  }));
  vi.mocked(useProjects).mockReturnValue({
    projects: [{ project_id: "p-beta", name: "Beta", work_dir: "", task_count: 1, active_count: 0 } as never],
    projectsRoot: "", error: "", reload: vi.fn(),
  });
  window.localStorage.clear();
  window.sessionStorage.clear();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("Work list", () => {
  it("uses Chinese for list controls and schedule status", async () => {
    await show();
    for (const text of ["新建任务", "查找任务", "管家", "进行中", "定时任务", "已结束", "连续失败 3 次"]) {
      expect(host.textContent + host.querySelector("input")?.getAttribute("placeholder")).toContain(text);
    }
    expect(host.textContent).not.toMatch(/New task|Manager|Schedules|Unseen|Failed|Running/);
  });

  it("full list reachable", async () => {
    await show();
    const ended = host.querySelector('[aria-label="已结束"]')!;
    await click(button(ended, "已结束"));
    await click(button(ended, "加载更多"));
    await click(button(ended, "加载更多"));
    const hrefs = [...ended.querySelectorAll("a")].map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual(["#work/task/e1", "#work/task/e2", "#work/task/e3", "#work/task/e4", "#work/task/e5"]);
    expect(new Set(hrefs).size).toBe(hrefs.length);
    expect(ended.textContent).not.toContain("加载更多");
  });

  it("sections and rows", async () => {
    await show();
    const open = host.querySelector('[aria-label="进行中任务"]')!;
    const headers = [...open.querySelectorAll("button")].map((item) => item.textContent ?? "");
    expect(headers[0]).toContain("Beta");
    expect(headers[1]).toContain("Alpha");
    const links = [...open.querySelectorAll("a")].map((link) => link.textContent ?? "");
    expect(links[0]).toContain("Beta new");
    expect(links[0]).toContain("运行中");
    expect(links[0]).toContain("未读");
    expect(links[1]).toContain("未命名任务");
    expect(links[1]).toContain("待开始");
    expect(links[2]).toContain("等你回复 (2)");
    expect(links[2].match(/等你回复/g)).toHaveLength(1);
    const openCalls = calls.filter((call) => call.url.includes("work_list") && call.body.section === "open");
    const scheduleCalls = calls.filter((call) => call.url.includes("work_list") && call.body.section === "schedules");
    expect(openCalls).toHaveLength(1);
    expect(scheduleCalls).toHaveLength(1);
    const schedules = host.querySelector('[aria-label="定时任务"]')!;
    expect(schedules.textContent).toContain("失败");
    expect(schedules.textContent).toContain("连续失败 3 次");
    expect(schedules.textContent).toContain("运行中");
    expect(schedules.querySelector("time")!.getAttribute("dateTime")).toBe("2026-11-24T01:00:00Z");
    await click(button(schedules, "Nightly"));
    expect(calls.some((call) => call.url.includes("/tasks/page") && call.body.schedule_id === "s1" && call.body.limit === 10)).toBe(true);
    expect(schedules.textContent).toContain("Nightly run");
  });

  it("degrades", async () => {
    failSchedules = true;
    missingDraft = true;
    missingSelect = true;
    await act(async () => { root.render(<WorkPage sub="" onNavigate={() => {}} />); });
    await settle();
    const open = host.querySelector('[aria-label="进行中任务"]')!;
    const schedules = host.querySelector('[aria-label="定时任务"]')!;
    expect(open.textContent).toContain("Beta new");
    expect(schedules.textContent).toContain("database unavailable");
    expect(schedules.querySelector('[role="alert"]')).not.toBeNull();
    failSchedules = false;
    await click(button(schedules, "重试"));
    expect(schedules.textContent).toContain("Nightly");
    expect(schedules.textContent).not.toContain("database unavailable");
    const draft = button(host, "新建任务");
    expect(draft.disabled).toBe(true);
    expect(host.textContent).toContain(DRAFTS_UNAVAILABLE);
    const search = host.querySelector('input[aria-label="查找任务"]') as HTMLInputElement;
    expect(search.disabled).toBe(true);
    expect(host.textContent).toContain(FIND_UNAVAILABLE);
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true })); });
    expect(host.querySelector('[role="dialog"]')).toBeNull();

    act(() => root.unmount());
    root = createRoot(host);
    missingList = true;
    missingDraft = false;
    missingSelect = false;
    await show();
    const again = host.querySelector('[aria-label="进行中任务"]')!;
    expect(again.textContent).toContain("此服务器尚不支持进行中任务。");
    expect(again.textContent).not.toContain("暂无进行中任务");
  });
});
