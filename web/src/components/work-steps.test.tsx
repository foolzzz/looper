// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkSteps } from "@/components/work-steps";
import type { GateState, TaskDetail, TaskStep, TaskWait, UserGate } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fetches: string[] = [];

function step(partial: Partial<TaskStep> & Pick<TaskStep, "id" | "step_key" | "step_index">): TaskStep {
  return {
    task_id: "task 1", kind: "work", instruction: "Do the work", acceptance_criteria: [], target_files: [],
    depends_on: [], status: "pending", review: false, role_key: null, role_version: null, review_roles: [],
    review_stage: null, review_position: null, retries: 0, worker_id: null, reviewer_id: null, result: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", completed_at: null, started_at: null,
    ...partial,
  };
}

function gate(stepId: string, state: GateState, waiting_on: UserGate["waiting_on"] = null): UserGate {
  return {
    step: stepId, step_id: stepId, state, topic: "Ask", agenda: [], findings: "", summary: null, resolution: "",
    ended_by_user: false, plan_change: null, depends_on: [], waiting_on, not_delivered: 0, retry_at: null,
    turns: 0, reminders: 0, opened_at: null, first_reply_at: null, closed_at: null, channel_posts: null,
  };
}

function wait(stepId: string, state: TaskWait["state"]): TaskWait {
  return {
    step: stepId, step_id: stepId, state, outcome: null, topic: "Wait", check: null, every_seconds: null,
    opened_at: null, due_at: null, deadline_at: null, next_check_at: null, last_checked_at: null, checks: 0,
    last_result: null, last_output: "", last_error: "", resolution: "", notice: null, closed_at: null, depends_on: [],
  };
}

function task(partial: Partial<TaskDetail> = {}): TaskDetail {
  return { task_id: "task 1", group: "in_progress", status: "running", steps: [], gates: [], waits: [], ...partial } as TaskDetail;
}

let host: HTMLDivElement;
let root: Root;

function show(value: TaskDetail, onOpenGate = vi.fn()) {
  act(() => { root.render(<WorkSteps task={value} onOpenGate={onOpenGate} />); });
  return { onOpenGate };
}

beforeEach(() => {
  fetches.length = 0;
  vi.stubGlobal("fetch", (url: string) => {
    fetches.push(String(url));
    throw new Error("work steps must not request");
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("WorkSteps", () => {
  it("says there is no plan until steps exist, then lists them in step_index order", () => {
    show(task());
    expect(host.querySelector("#work-steps-title")?.textContent).toBe("计划与步骤");
    expect(host.textContent).toContain("尚无步骤");
    expect(host.querySelector("[data-step-progress]")).toBeNull();
    expect(host.querySelectorAll("[data-step-key]")).toHaveLength(0);

    show(task({ steps: undefined as unknown as TaskStep[] }));
    expect(host.textContent).toContain("尚无步骤");

    show(task({
      steps: [
        step({ id: "c", step_key: "third", step_index: 2, depends_on: [] }),
        step({ id: "a", step_key: "first", step_index: 0, depends_on: [] }),
        step({ id: "b", step_key: "second", step_index: 1, depends_on: ["a"] }),
      ],
    }));
    expect(host.textContent).not.toContain("尚无步骤");
    expect([...host.querySelectorAll("[data-step-key]")].map((row) => row.getAttribute("data-step-key"))).toEqual([
      "first", "second", "third",
    ]);
    const rows = [...host.querySelectorAll("[data-step-key]")];
    expect(rows[0].textContent).toContain("无依赖");
    expect(rows[1].textContent).toContain("依赖 first");
    expect(rows[1].textContent).not.toContain("依赖 a");
    expect(rows[2].textContent).toContain("无依赖");
  });

  it("expands the original instruction and acceptance text", () => {
    const instruction = "核对退款终态\nVerify every terminal state " + "x".repeat(80) + "TAIL-MARKER";
    const criteria = ["保持原文\n第二行", `repo/services/refund/${"p".repeat(60)}.go`];
    show(task({
      steps: [step({
        id: "a", step_key: "refund", step_index: 0, instruction, acceptance_criteria: criteria,
        target_files: [`repo/services/refund/${"p".repeat(80)}.go`],
      })],
    }));
    const instructionBox = host.querySelector("[data-instruction]") as HTMLDetailsElement;
    expect(instructionBox.open).toBe(false);
    expect(instructionBox.querySelector("summary")?.textContent).not.toContain("TAIL-MARKER");
    expect(instructionBox.querySelector("[data-instruction-full]")?.textContent).toBe(instruction);
    instructionBox.open = true;
    expect(instructionBox.querySelector("[data-instruction-full]")?.textContent).toBe(instruction);

    const acceptance = host.querySelector("[data-acceptance]") as HTMLDetailsElement;
    expect(acceptance.open).toBe(false);
    acceptance.open = true;
    const items = [...acceptance.querySelectorAll("li")].map((item) => item.textContent);
    expect(items).toEqual(criteria);
    expect(host.querySelector("[data-target-file]")?.textContent).toBe(`repo/services/refund/${"p".repeat(80)}.go`);
    expect(host.querySelector("[data-work-steps]")?.className).toContain("min-w-0");
    expect(host.querySelector("[data-work-steps]")?.className).toContain("overflow-x-auto");
    expect(host.querySelector("[data-target-file]")?.className).toContain("[overflow-wrap:anywhere]");
    expect(host.querySelector("[data-instruction-full]")?.className).toContain("[overflow-wrap:anywhere]");
    expect(host.querySelector("script")).toBeNull();
    expect(fetches).toEqual([]);
  });

  it.each([
    ["pending", "未开始", "0/1 已完成"],
    ["dispatched", "已派出", "0/1 已完成"],
    ["running", "运行中", "0/1 已完成"],
    ["deciding", "待决定", "0/1 已完成"],
    ["waiting_user", "等你处理", "0/1 已完成"],
    ["consulting", "请教中", "0/1 已完成"],
    ["completed", "评审中", "0/1 已完成 · 1 评审中"],
    ["verified", "已完成", "1/1 已完成"],
    ["failed", "失败", "0/1 已完成"],
    ["skipped", "已跳过", "0/0 已完成 · 1 已跳过"],
  ] as const)("status %s reads %s and counts %s", (status, label, progress) => {
    show(task({ steps: [step({ id: "a", step_key: "build", step_index: 0, status })] }));
    expect(host.querySelector("[data-status-text]")?.textContent).toBe(label);
    expect(host.querySelector("[data-step-progress]")?.textContent).toBe(progress);
    if (status !== "completed") expect(host.querySelector("[data-status-text]")?.textContent).not.toBe("评审中");
    expect(fetches).toEqual([]);
  });

  it("keeps an unknown status out of the done count and does not match on substrings", () => {
    show(task({ steps: [step({ id: "a", step_key: "odd", step_index: 0, status: "incomplete" })] }));
    expect(host.querySelector("[data-status-text]")?.textContent).toBe("未知状态 incomplete");
    expect(host.querySelector("[data-step-progress]")?.textContent).toBe("0/1 已完成");
    expect(host.textContent).not.toContain("评审中");

    show(task({ steps: [step({ id: "a", step_key: "odd", step_index: 0, status: "mystery" })] }));
    expect(host.querySelector("[data-status-text]")?.textContent).toBe("未知状态 mystery");
    expect(host.querySelector("[data-step-progress]")?.textContent).toBe("0/1 已完成");
  });

  it("counts like the task page and opens a gate in place while other steps keep the pipeline link", () => {
    const onOpenGate = vi.fn();
    show(task({
      task_id: "task 1",
      group: "in_progress",
      steps: [
        step({ id: "w1", step_key: "build", step_index: 0, kind: "work", status: "verified" }),
        step({ id: "w2", step_key: "check", step_index: 1, kind: "work", status: "completed" }),
        step({ id: "w3", step_key: "drop", step_index: 2, kind: "work", status: "skipped" }),
        step({ id: "g1", step_key: "ask/now", step_index: 3, kind: "gate", status: "verified", instruction: "Decide" }),
        step({ id: "wait-1", step_key: "hold", step_index: 4, kind: "wait", status: "pending" }),
        step({ id: "r1", step_key: "panel", step_index: 5, kind: "review", status: "failed" }),
      ],
      gates: [gate("g1", "cancelled")],
      waits: [wait("wait-1", "timed_out")],
    }), onOpenGate);

    expect(host.querySelector("[data-step-progress]")?.textContent).toBe(
      "1/4 已完成 · 1 评审中 · 1 已跳过 · 1 个关卡已取消",
    );
    const gateRow = host.querySelector('[data-step-key="ask/now"]')!;
    expect(gateRow.textContent).toContain("Cancelled");
    expect(gateRow.querySelector("a")).toBeNull();
    const gateButton = gateRow.querySelector("button")!;
    expect(gateButton.type).toBe("button");
    gateButton.focus();
    expect(document.activeElement).toBe(gateButton);
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    gateButton.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(false);
    gateButton.click();
    expect(onOpenGate).toHaveBeenCalledTimes(1);
    expect(onOpenGate.mock.calls[0][0]).toMatchObject({ id: "g1", kind: "gate", step_key: "ask/now" });
    expect(window.location.hash).not.toContain("ask");

    const work = host.querySelector('[data-step-key="build"]')!.querySelector("a")!;
    const expected = `#tasks/${encodeURIComponent("task 1")}?${new URLSearchParams({ tab: "pipeline", step: "w1" })}`;
    expect(work.getAttribute("href")).toBe(expected);
    expect(work.textContent).toBe("build");
    expect(work.getAttribute("href")).toContain(encodeURIComponent("task 1"));
    work.focus();
    expect(document.activeElement).toBe(work);
    const key = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    work.dispatchEvent(key);
    expect(key.defaultPrevented).toBe(false);
    expect(work.tabIndex).toBeGreaterThanOrEqual(0);

    expect(host.querySelector('[data-step-key="check"] [data-status-text]')?.textContent).toBe("评审中");
    expect(host.querySelector('[data-step-key="hold"]')?.textContent).toContain("Timed out");
    expect(host.querySelector('[data-step-key="hold"]')?.textContent).not.toContain("未开始");
    expect(host.querySelector('[data-step-key="panel"] [data-status-text]')?.textContent).toBe("失败");
    expect([...host.querySelectorAll("[data-kind]")].map((row) => row.getAttribute("data-kind"))).toEqual([
      "work", "work", "work", "gate", "wait", "review",
    ]);
    for (const kind of ["work", "gate", "wait", "review"]) {
      expect(host.querySelector(`[data-kind="${kind}"] svg`)).not.toBeNull();
    }
    expect(fetches).toEqual([]);
  });

  it("uses the gate's own words, including whose turn it is", () => {
    show(task({
      group: "in_progress",
      steps: [step({ id: "g1", step_key: "ask", step_index: 0, kind: "gate", status: "waiting_user" })],
      gates: [gate("g1", "discussing", "planner")],
    }));
    expect(host.querySelector('[data-step-key="ask"]')?.textContent).toContain("Planner answering");

    show(task({
      group: "completed",
      steps: [step({ id: "g1", step_key: "ask", step_index: 0, kind: "gate", status: "waiting_user" })],
      gates: [gate("g1", "discussing", "planner")],
    }));
    expect(host.querySelector('[data-step-key="ask"]')?.textContent).toContain("Your turn");
    expect(host.querySelector('[data-step-key="ask"]')?.textContent).not.toContain("Planner answering");
  });

  it("renders one task's steps at a time and leaves markup as text", () => {
    show(task({ steps: [step({ id: "a", step_key: "alpha", step_index: 0, instruction: "<img src=x onerror=alert(1)>" })] }));
    expect(host.textContent).toContain("alpha");
    expect(host.querySelector("img")).toBeNull();
    show(task({ steps: [step({ id: "b", step_key: "beta", step_index: 0 })] }));
    expect(host.textContent).not.toContain("alpha");
    expect(host.textContent).toContain("beta");
    expect(fetches).toEqual([]);
  });

  it("gives instruction and acceptance summaries a 44px coarse target and toggles them from the keyboard", () => {
    const dom = (window as unknown as { happyDOM: { settings: { navigator: { maxTouchPoints: number } } } }).happyDOM;
    dom.settings.navigator.maxTouchPoints = 1;
    const style = document.createElement("style");
    style.textContent = "@media (pointer: coarse) { .pointer-coarse\\:min-h-11 { min-height: 44px; } }";
    document.head.append(style);
    const instruction = "核对退款终态\n" + "x".repeat(90);
    show(task({
      steps: [step({
        id: "a", step_key: "refund", step_index: 0, instruction, acceptance_criteria: ["保持原文"],
      })],
    }));
    const boxes = [...host.querySelectorAll("[data-instruction] summary, [data-acceptance] summary")] as HTMLElement[];
    expect(boxes).toHaveLength(2);
    expect(boxes.map((box) => box.textContent)).toEqual([expect.stringContaining("展开"), "验收"]);
    for (const summary of boxes) {
      expect(summary.className).toContain("pointer-coarse:min-h-11");
      const minHeight = parseFloat(window.getComputedStyle(summary).minHeight);
      expect(minHeight).toBeGreaterThanOrEqual(44);
      const details = summary.parentElement as HTMLDetailsElement;
      expect(details.open).toBe(false);
      summary.focus();
      expect(document.activeElement).toBe(summary);
      for (const key of ["Enter", " "] as const) {
        const down = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
        summary.dispatchEvent(down);
        expect(down.defaultPrevented).toBe(false);
      }
      // Enter and Space activate a summary through a click. happy-dom toggles <details> on that click.
      summary.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      expect(details.open).toBe(true);
      summary.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      expect(details.open).toBe(false);
    }
    style.remove();
    dom.settings.navigator.maxTouchPoints = 0;
  });
});
