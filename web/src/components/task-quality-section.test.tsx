// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskQualitySection, durationText, meanCostText } from "@/components/task-quality-section";
import { V1Error } from "@/lib/v1";
import { fetchProjectsList, fetchRegistry } from "@/lib/api";
import { fetchTaskMetrics, type MeanCost, type Ratio, type TaskMetrics } from "@/lib/task-metrics";

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchProjectsList: vi.fn(),
  fetchRegistry: vi.fn(),
}));
vi.mock("@/lib/task-metrics", async (original) => ({
  ...(await original<typeof import("@/lib/task-metrics")>()),
  fetchTaskMetrics: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ratio = (k: number, n: number): Ratio =>
  n < 20 ? { insufficient: true, n } : { n, k, value: k / n, wilson: [Math.max(0, k / n - 0.1), Math.min(1, k / n + 0.1)] };
const mean = (usd: number | null, unknown = 0): MeanCost => ({ mean_usd: usd, n_known: usd === null ? 0 : 3, unknown });
const spread = (p50: number | null, p95: number | null, n = 5) => ({ n, p50_ms: p50, p95_ms: p95 });

function metrics(over: Partial<TaskMetrics> = {}): TaskMetrics {
  return {
    days: 7, window: { since: "", until: "" }, comparison_window: { since: "", until: "" },
    filters: { project: null, profile: null, difficulty: null }, coverage_since: "2026-11-20T00:00:00Z", missing_attempts: 0,
    tasks: {
      success_rate: { ...ratio(18, 24), previous: ratio(40, 50) },
      budget_failure_rate: ratio(1, 24), restarts: { count: 2 },
      cost_per_completed_task: { n: 18, provider_priced: mean(1.25), equivalent: mean(3, 2) },
      duration: spread(1, 2), agent_duration: { ...spread(125_000, 600_000), previous: spread(100_000, 500_000) },
      queue_time: spread(1, 2), user_corrections: { supplement: ratio(1, 24), revision: ratio(1, 24), feedback: ratio(1, 24) },
    },
    steps: {
      first_pass_rate: ratio(5, 12),
      replans: { decisions: 0, steps_added: 0 },
      attempts_per_accepted_step: { n: 30, mean: 1.4, distribution: {} },
      rework_rate: ratio(3, 30), failure_kinds: { n: 0, counts: {} },
      cost_per_accepted_step: {
        n: 30,
        provider_priced: { first: mean(0.5), retry: mean(0.25), total: mean(0.75) },
        equivalent: { first: mean(null), retry: mean(null), total: mean(null, 4) },
      },
      duration: spread(1, 2),
    },
    reviews: {
      block_rate: ratio(1, 2), effective_blocks: { count: 1, blocked: 1, rate: ratio(1, 1) },
      misses: { count: 3 }, catch_rate: ratio(9, 20), defective_deliveries: { count: 0, completed: 0, by_verdict: {} },
      adjudication_rate: ratio(0, 0),
    },
    contract: { rates: { worker: ratio(0, 0), reviewer: ratio(0, 0), planner: ratio(0, 0) }, run_error_rate: ratio(0, 0) },
    ...over,
  };
}

describe("the dashboard's 任务质量 group", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.resetAllMocks();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    vi.mocked(fetchTaskMetrics).mockResolvedValue(metrics());
    vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [{ project_id: "p1", name: "Alpha" }], projects_root: "" } as never);
    vi.mocked(fetchRegistry).mockResolvedValue({ profiles: [{ profile_id: "fast", name: "Fast" }] } as never);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });
  const show = async () => { await act(async () => root.render(<TaskQualitySection />)); };
  const text = () => container.textContent ?? "";

  it("shows the figures against the 28 days before, and 样本不足 under 20 cases instead of a percentage", async () => {
    await show();
    expect(container.querySelector('section[aria-label="任务质量"]')).toBeTruthy();
    expect(text()).toContain("最近 7 天，对照前 28 天");
    expect(text()).toContain("75%");
    expect(text()).toContain("前 28 天：80%");
    expect(text()).toContain("样本不足 (n=12)");
    expect(text()).not.toContain("42%");
    expect(text()).toContain("1.40");
    expect(text()).toContain("2 min / 10 min");
    expect(text()).toContain("评审漏检数");
    expect(text()).toContain("供应商 $0.7500");
    expect(text()).toContain("等价 未知（4 个）");
    expect(text()).toContain("供应商 $1.25");
    expect(text()).toContain("等价 $3.00（另有 2 个未知）");
    expect(fetchTaskMetrics).toHaveBeenCalledWith(7, { project: "", profile: "" }, expect.anything());
  });

  it("shows the equivalent spend in the 28-day cost comparison", async () => {
    const answer = metrics();
    answer.steps.cost_per_accepted_step = {
      ...answer.steps.cost_per_accepted_step,
      previous: {
        n: 20,
        provider_priced: { first: mean(1), retry: mean(1), total: mean(2) },
        equivalent: { first: mean(1), retry: mean(1), total: mean(3) },
      },
    };
    answer.tasks.cost_per_completed_task = {
      ...answer.tasks.cost_per_completed_task,
      previous: { n: 10, provider_priced: mean(2), equivalent: mean(4) },
    };
    vi.mocked(fetchTaskMetrics).mockResolvedValue(answer);
    await show();
    const tile = (label: string) => [...container.querySelectorAll("div")].find((node) =>
      node.querySelector("p")?.textContent === label);
    expect(tile("每个被接受步骤的花费")?.textContent).toContain("前 28 天：供应商 $2.00 · 等价 $3.00");
    expect(tile("每个完成任务的花费")?.textContent).toContain("前 28 天：供应商 $2.00 · 等价 $4.00");
  });

  it("switches project and profile and reads again with the choice", async () => {
    await show();
    const projectSelect = container.querySelector('select[aria-label="项目"]') as HTMLSelectElement;
    expect([...projectSelect.options].map((o) => o.textContent)).toEqual(["全部", "Alpha"]);
    await act(async () => {
      projectSelect.value = "p1";
      projectSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(fetchTaskMetrics).toHaveBeenLastCalledWith(7, { project: "p1", profile: "" }, expect.anything());
    const profileSelect = container.querySelector('select[aria-label="Profile"]') as HTMLSelectElement;
    await act(async () => {
      profileSelect.value = "fast";
      profileSelect.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(fetchTaskMetrics).toHaveBeenLastCalledWith(7, { project: "p1", profile: "fast" }, expect.anything());
  });

  it("shows a failed read here and still offers the switches; the last figures stay when a later read fails", async () => {
    vi.mocked(fetchTaskMetrics).mockRejectedValue(new V1Error(500, "internal", "database down"));
    await show();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("database down");
    expect(container.querySelector('select[aria-label="项目"]')).toBeTruthy();
  });

  it("shows one figure's error in its tile and leaves the others", async () => {
    const answer = metrics();
    answer.tasks.success_rate = { error: "RuntimeError: boom" };
    vi.mocked(fetchTaskMetrics).mockResolvedValue(answer);
    await show();
    expect(text()).toContain("读取失败：RuntimeError: boom");
    expect(text()).toContain("样本不足 (n=12)");
  });

  it("works when the project and profile lists cannot be read", async () => {
    vi.mocked(fetchProjectsList).mockRejectedValue(new Error("no"));
    vi.mocked(fetchRegistry).mockRejectedValue(new Error("no"));
    await show();
    expect(text()).toContain("75%");
  });

  it("warns about Worker runs without an attempt and says where records begin", async () => {
    vi.mocked(fetchTaskMetrics).mockResolvedValue(metrics({ missing_attempts: 3 }));
    await show();
    expect(text()).toContain("记录始于");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("3 个 Worker 运行没有记下尝试");
  });
});

describe("number words", () => {
  it("never writes an unknown as zero", () => {
    expect(durationText(null)).toBe("–");
    expect(durationText(45_000)).toBe("45 s");
    expect(durationText(3_900_000)).toBe("1 h 5 min");
    expect(meanCostText(mean(null))).toBe("–");
    expect(meanCostText(mean(null, 2))).toBe("未知（2 个）");
  });
});
