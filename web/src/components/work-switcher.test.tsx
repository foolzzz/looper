// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProjects } from "@/lib/use-projects";
import { WorkPage } from "@/pages/work";
import { SELECT_DEBOUNCE_MS } from "@/components/work-switcher";

vi.mock("@/lib/use-projects", () => ({ useProjects: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Answer { status: number; body: unknown }
const calls: { url: string; body: Record<string, unknown>; signal?: AbortSignal }[] = [];
let selectResult: unknown = { kind: "none", searched: { fields: ["title"], window_days: 14, all: false }, hint: "nothing matched" };
let host: HTMLDivElement;
let root: Root;
const navigated: string[] = [];

function Harness() {
  const [sub, setSub] = useState("");
  return (
    <WorkPage sub={sub} onNavigate={(next) => {
      navigated.push(next);
      window.location.hash = next ? `#work/${next}` : "#work";
      setSub(next);
    }} />
  );
}

async function settle() {
  for (let i = 0; i < 4; i += 1) await act(async () => { await Promise.resolve(); });
}
function type(input: HTMLInputElement, value: string) {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  set.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(() => {
  vi.useFakeTimers();
  calls.length = 0;
  navigated.length = 0;
  selectResult = {
    kind: "candidates",
    more: 3,
    candidates: [
      { task_id: "c1", title: "Payment", project_name: "Beta", status: "running", group: "in_progress", last_activity_at: null },
      { task_id: "c2", title: "Payroll", project_name: "Beta", status: "running", group: "in_progress", last_activity_at: null },
    ],
  };
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    calls.push({ url: String(url), body, signal: init?.signal ?? undefined });
    if (String(url).includes("tasks/select") && body.selection === "p") {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    }
    if (String(url).includes("tasks/detail")) return Promise.resolve(Response.json({ status: "ok", result: {
      task_id: body.task_id, title: `Selected ${body.task_id}`, project_id: "p-beta", project_name: "Beta", status: "running", group: "in_progress",
    } }));
    if (String(url).includes("tasks/activity")) return Promise.resolve(Response.json({ status: "ok", result: { activity: [{ seq: 901 }] } }));
    const answer: Answer = method === "GET"
      ? { status: 405, body: { status: "error", error: { code: "invalid", message: "use POST" } } }
      : String(url).includes("tasks/select")
        ? { status: 200, body: { status: "ok", result: selectResult } }
        : String(url).includes("tasks/work_list")
          ? { status: 200, body: { status: "ok", result: { items: [], schedules: [], next_cursor: null } } }
          : { status: 200, body: { status: "ok", result: {} } };
    return Promise.resolve(new Response(JSON.stringify(answer.body), { status: answer.status, headers: { "Content-Type": "application/json" } }));
  }));
  vi.mocked(useProjects).mockReturnValue({
    projects: [{ project_id: "p-beta", name: "Beta", work_dir: "", task_count: 0, active_count: 0 } as never],
    projectsRoot: "", error: "", reload: vi.fn(),
  });
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "#work");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Work quick switcher", () => {
  it("cannot choose results from an earlier query while a new query is pending", async () => {
    await act(async () => { root.render(<Harness />); });
    await settle();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const dialog = () => host.querySelector('[role="dialog"]') as HTMLElement;
    const input = () => dialog().querySelector("input") as HTMLInputElement;
    const enter = async () => {
      await act(async () => { input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    };

    await act(async () => { type(input(), "pay"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS); });
    expect(dialog().querySelectorAll('[role="option"]')).toHaveLength(2);

    // The new query invalidates visible rows before its debounce or request completes.
    await act(async () => { type(input(), "p"); });
    await enter();
    expect(navigated).toEqual([]);
    expect(dialog().querySelectorAll('[role="option"]')).toHaveLength(0);
    expect(dialog().querySelector('[role="status"]')?.textContent).toContain("正在加载");
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS); });
    await enter();
    expect(navigated).toEqual([]);

    const slow = calls.find((call) => call.url.includes("tasks/select") && call.body.selection === "p")!;
    await act(async () => { type(input(), "different"); });
    expect(slow.signal?.aborted).toBe(true);
    await enter();
    expect(navigated).toEqual([]);
    selectResult = { kind: "one", task: { task_id: "fresh", title: "Different", project_name: "Beta", status: "running", group: "in_progress", last_activity_at: null } };
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS); });
    expect(dialog().querySelector('[role="status"]')).toBeNull();
    await enter();
    expect(navigated).toEqual(["task/fresh"]);
  });

  it("quick switcher", async () => {
    await act(async () => { root.render(<Harness />); });
    await settle();
    const opener = [...host.querySelectorAll("button")].find((item) => item.textContent === "新建任务") as HTMLButtonElement;
    opener.focus();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const dialog = () => host.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog()).not.toBeNull();
    const input = () => dialog().querySelector("input") as HTMLInputElement;

    await act(async () => { type(input(), "p"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS); });
    const hung = calls.filter((call) => call.url.includes("tasks/select") && call.body.selection === "p");
    expect(hung).toHaveLength(1);
    await act(async () => { type(input(), "pa"); });
    expect(hung[0].signal?.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS); });
    expect(calls.filter((call) => call.url.includes("tasks/select") && call.body.selection === "pa")).toHaveLength(1);

    await act(async () => { input().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await act(async () => { await Promise.resolve(); });
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);

    opener.focus();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const before = calls.filter((call) => call.url.includes("tasks/select")).length;
    await act(async () => { type(input(), "pay"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS - 1); });
    expect(calls.filter((call) => call.url.includes("tasks/select") && call.body.selection === "pay")).toHaveLength(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(calls.filter((call) => call.url.includes("tasks/select") && call.body.selection === "pay")).toHaveLength(1);
    expect(calls.filter((call) => call.url.includes("tasks/select")).length).toBe(before + 1);
    expect(dialog().textContent).toContain("还有 3 项未显示");
    const options = () => [...dialog().querySelectorAll('[role="option"]')];
    expect(options()[0].getAttribute("aria-selected")).toBe("true");
    await act(async () => { input().dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); });
    expect(options()[1].getAttribute("aria-selected")).toBe("true");
    expect(navigated).not.toContain("task/c2");
    await act(async () => { input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(navigated).toContain("task/c2");
    expect(window.location.hash).toBe("#work/task/c2");
    await settle();
    expect(host.querySelector("h1")?.textContent).toBe("Selected c2");
    expect(calls.some(call => call.url.includes("mark_seen") && call.body.task_id === "c2" && call.body.seq === 901)).toBe(true);

    selectResult = { kind: "one", task: { task_id: "only", title: "Only one", project_name: "Beta", status: "running", group: "in_progress", last_activity_at: null } };
    opener.focus();
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { type(input(), "only"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS); });
    expect(dialog().querySelector('[role="option"]')!.getAttribute("aria-selected")).toBe("true");
    expect(navigated).not.toContain("task/only");
    await act(async () => { input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(navigated).toContain("task/only");

    selectResult = { kind: "none", searched: { fields: ["title", "mr"], window_days: 14, all: false }, hint: "nothing matched" };
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { type(input(), "zzz"); });
    await act(async () => { await vi.advanceTimersByTimeAsync(SELECT_DEBOUNCE_MS); });
    const text = dialog().textContent ?? "";
    expect(text).toContain("fields");
    expect(text).toContain("window_days");
    expect(text).toContain("all");
    expect(text).toContain("nothing matched");
    expect(text).toContain("14");
  });
});
