// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkTaskLink } from "@/components/work-row";
import { taskTitle, type WorkTask } from "@/lib/work";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
const base: WorkTask = {
  task_id: "task-example", title: "确认数据库方案与输出结果", project_id: "example", project_name: "Example",
  status: "waiting_user", group: "needs_you", last_activity_at: "2026-11-25T10:20:00Z",
  last_seq: 1, waiting_gates: 1, needs_you: true, unseen: false,
};

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("Work task row", () => {
  it("gives the complete title its own first line and shows waiting once", () => {
    act(() => root.render(<WorkTaskLink task={base} current onActivate={vi.fn()} />));
    const link = host.querySelector("a")!;
    const title = link.firstElementChild as HTMLElement;
    expect(title.textContent).toBe(base.title);
    expect(title.classList.contains("w-full")).toBe(true);
    expect(title.classList.contains("truncate")).toBe(true);
    expect(link.textContent?.match(/等你回复/g)).toHaveLength(1);
    expect(link.textContent).not.toMatch(/Waits for you|Waiting for you/);
    expect(link.querySelector('span[aria-hidden="true"]')?.classList.contains("bg-warning")).toBe(true);
    expect(link.getAttribute("aria-current")).toBe("page");
    expect(link.className).toContain("focus-visible:ring-1");
    expect(link.className).toContain("bg-primary/10");
    link.focus();
    expect(document.activeElement).toBe(link);
  });

  it("uses the local creation time for an untitled task without displaying its id", () => {
    const created = "2026-11-25T10:20:00Z";
    const local = new Date(created).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    act(() => root.render(<WorkTaskLink task={{ ...base, title: base.task_id, created_at: created }} current={false} onActivate={vi.fn()} />));
    const title = host.querySelector("a > span")!.textContent;
    expect(title).toBe(`未命名任务 · ${local}`);
    expect(title).not.toContain(base.task_id);
    expect(taskTitle("", created)).toBe(title);
  });
});
