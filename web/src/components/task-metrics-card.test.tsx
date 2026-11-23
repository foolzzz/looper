// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskMetricsCard } from "@/components/task-metrics-card";
import type { Cost, CostGroup, StepAttempts, TaskFeedback } from "@/lib/task-metrics";
import { V1Error } from "@/lib/v1";
import * as metrics from "@/lib/task-metrics";

vi.mock("@/lib/task-metrics", async (original) => ({
  ...(await original<typeof import("@/lib/task-metrics")>()),
  fetchStepAttempts: vi.fn(),
  fetchTaskFeedback: vi.fn(),
  setTaskFeedback: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const basis = (usd: number | null, runs = 1, unknown_runs = 0) => ({ usd, runs, unknown_runs });
const cost = (paid: ReturnType<typeof basis>, equivalent = basis(0, 0)): Cost => ({ provider_priced: paid, equivalent });
const group = (worker: Cost, review: Cost, advisor: Cost, total: Cost): CostGroup => ({ worker, review, advisor, total });
const none = cost(basis(0, 0));

function view(over: Partial<StepAttempts> = {}): StepAttempts {
  return {
    task_id: "t1", status: "completed", run_no: 1, coverage_since: "2026-11-20T00:00:00Z", missing_attempts: 0, steps: [],
    summary: { steps: 4, attempts: 6, accepted_steps: 4, first_pass_steps: 2, open_attempts: 0, failure_kinds: { review_rejected: 2 } },
    costs: {
      first: group(cost(basis(1.5)), cost(basis(0.25)), none, cost(basis(1.75))),
      retry: group(cost(basis(0.5)), none, cost(basis(0.1)), cost(basis(0.6))),
      planning: cost(basis(0.4)),
      unattributed: group(none, none, cost(basis(2)), cost(basis(2))),
      total: cost(basis(4.75)),
    },
    ...over,
  };
}

describe("the task page's 指标 card", () => {
  let container: HTMLDivElement;
  let root: Root;
  const feedback = (over: Partial<TaskFeedback> = {}): TaskFeedback =>
    ({ task_id: "t1", verdict: null, note: null, created_at: null, updated_at: null, ...over });

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(metrics.fetchStepAttempts).mockResolvedValue(view());
    vi.mocked(metrics.fetchTaskFeedback).mockResolvedValue(feedback());
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });
  const show = async (props: Partial<Parameters<typeof TaskMetricsCard>[0]> = {}) => {
    await act(async () => root.render(<TaskMetricsCard taskId="t1" status="completed" active={false} revision="a" {...props} />));
  };
  const text = () => container.textContent ?? "";
  const press = async (label: string) => {
    const button = [...container.querySelectorAll("button")].find((b) => b.textContent === label);
    expect(button, label).toBeTruthy();
    await act(async () => button!.click());
  };

  it("shows first-pass steps, attempts, the cost split and the failure reasons in words", async () => {
    await show();
    expect(text()).toContain("指标");
    expect(text()).toContain("2 / 4");
    expect(text()).toContain("6");
    expect(text()).toContain("评审未通过");
    expect(text()).toContain("×2");
    const rows = [...container.querySelectorAll("tbody tr")].map((r) => r.textContent ?? "");
    expect([...container.querySelectorAll("tbody th")].map((th) => th.textContent)).toEqual(["首次尝试", "重试", "规划", "未归属", "合计"]);
    expect(rows[0]).toContain("$1.75");
    expect(rows[0]).toContain("执行 $1.50");
    expect(rows[0]).toContain("评审 $0.2500");
    expect(rows[1]).toContain("advisor $0.1000");
    expect(rows[3]).toContain("$2.00");
    expect(text()).toContain("按供应商价格");
    expect(text()).toContain("等价花费");
    expect(text()).not.toContain("进行中");
  });

  it("shows an unknown cost as 未知 and a partly unknown one with a plus, never as zero", async () => {
    vi.mocked(metrics.fetchStepAttempts).mockResolvedValue(view({
      costs: { ...view().costs, planning: cost(basis(null, 2, 2)), total: cost(basis(1.25, 4, 1)) },
    }));
    await show();
    const rows = [...container.querySelectorAll("tbody tr")];
    expect(rows[2].textContent).toContain("未知");
    expect(rows[2].textContent).not.toContain("$0");
    expect(rows[4].textContent).toContain("$1.25+");
    expect(rows[4].querySelector("[title]")?.getAttribute("title")).toContain("1");
  });

  it("marks a running task as 进行中 and reads the card again when the page loads the task again", async () => {
    await show({ status: "running", active: true });
    expect(text()).toContain("进行中");
    expect(metrics.fetchStepAttempts).toHaveBeenCalledTimes(1);
    expect(metrics.fetchTaskFeedback).not.toHaveBeenCalled();
    await show({ status: "running", active: true, revision: "b" });
    expect(metrics.fetchStepAttempts).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[data-testid="delivery-feedback"]')).toBeNull();
  });

  it("keeps the spend table and the missing-attempt warning when there are no attempts, at 390px", async () => {
    vi.stubGlobal("innerWidth", 390);
    container.style.width = "390px";
    vi.mocked(metrics.fetchStepAttempts).mockResolvedValue(view({
      summary: { steps: 0, attempts: 0, accepted_steps: 0, first_pass_steps: 0, open_attempts: 0, failure_kinds: {} },
      missing_attempts: 1,
    }));
    await show();
    expect(window.innerWidth).toBe(390);
    expect(text()).toContain("没有尝试记录");
    expect(text()).toContain("未归属");
    expect(text()).toContain("$2.00");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("1 个 Worker 运行没有记下尝试");
    expect(text()).not.toContain("一次通过的步骤");
    const table = container.querySelector("table");
    expect(table?.parentElement?.className).toContain("overflow-x-auto");
    expect(container.querySelector("section")!.scrollWidth).toBeLessThanOrEqual(390);
  });

  it("says so when the task has no attempt records, and warns when a write was missed", async () => {
    vi.mocked(metrics.fetchStepAttempts).mockResolvedValue(view({
      summary: { steps: 0, attempts: 0, accepted_steps: 0, first_pass_steps: 0, open_attempts: 0, failure_kinds: {} },
    }));
    await show();
    expect(text()).toContain("没有尝试记录");
    vi.mocked(metrics.fetchStepAttempts).mockResolvedValue(view({ missing_attempts: 2 }));
    await show({ revision: "b" });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("2 个 Worker 运行没有记下尝试");
  });

  it("keeps the last numbers and names the error when a read fails", async () => {
    await show();
    vi.mocked(metrics.fetchStepAttempts).mockRejectedValue(new V1Error(500, "internal", "boom"));
    await show({ revision: "b" });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("boom");
    expect(text()).toContain("2 / 4");
  });

  it("offers 交付有问题 on a completed task only, and writes the verdict with its note", async () => {
    vi.mocked(metrics.setTaskFeedback).mockResolvedValue(feedback({ verdict: "wrong_approach", note: "wrong layer" }));
    await show();
    await press("交付有问题");
    const choice = [...container.querySelectorAll('input[type="radio"]')][2] as HTMLInputElement;
    await act(async () => choice.click());
    const note = container.querySelector("textarea")!;
    expect(note.maxLength).toBe(500);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(note, "wrong layer");
      note.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press("保存");
    expect(metrics.setTaskFeedback).toHaveBeenCalledWith("t1", "wrong_approach", "wrong layer");
    expect(text()).toContain("交付有问题：方案不对");
    expect(text()).toContain("wrong layer");
  });

  it("withdraws a mark, and is absent for someone who may not read it", async () => {
    vi.mocked(metrics.fetchTaskFeedback).mockResolvedValue(feedback({ verdict: "defect" }));
    vi.mocked(metrics.setTaskFeedback).mockResolvedValue(feedback());
    await show();
    expect(text()).toContain("交付有问题：有缺陷");
    await press("撤销标记");
    expect(metrics.setTaskFeedback).toHaveBeenCalledWith("t1", null, "");
    expect(text()).not.toContain("交付有问题：");
    expect([...container.querySelectorAll("button")].map((b) => b.textContent)).toContain("交付有问题");

    vi.mocked(metrics.fetchTaskFeedback).mockRejectedValue(new V1Error(403, "forbidden", "operator only"));
    await act(async () => root.render(<TaskMetricsCard taskId="t2" status="completed" active={false} revision="a" />));
    expect(container.querySelector('[data-testid="delivery-feedback"]')).toBeNull();
    expect(text()).toContain("指标");
  });

  it("shows a refused write and stays open", async () => {
    vi.mocked(metrics.setTaskFeedback).mockRejectedValue(new V1Error(409, "conflict", "only a completed task takes a verdict"));
    await show();
    await press("交付有问题");
    await press("保存");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("only a completed task");
    expect(container.querySelector("form")).toBeTruthy();
  });
});
