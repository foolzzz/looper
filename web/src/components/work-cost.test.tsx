// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { WorkCost } from "@/components/work-cost";
import { TaskSpendMeter } from "@/components/task-budget";
import type { TaskCost, TaskModelCost } from "@/lib/api";

const MODEL: TaskModelCost = {
  model: "task-model", runs: 1, running: 0, unpriced_runs: 0, interrupted_runs: 0,
  input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_creation_tokens: 0,
  cache_reported_runs: 1, cache_missing_runs: 0, cache_hit_prompt_tokens: 10, cache_hit_read_tokens: 0,
  cost_usd: 2,
};
const COST: TaskCost = {
  cost_usd: 2, runs: 1, running: 0, unpriced_runs: 0, provider_price_runs: 1,
  by_role: { planner: { cost_usd: 2, runs: 1 }, worker: { cost_usd: 0, runs: 0 }, reviewer: { cost_usd: 0, runs: 0 } },
  by_model: [MODEL], manager_by_model: [],
};
const show = (cost: TaskCost | null | undefined, cap: number | null = null, own: number | null = null) => {
  const host = document.createElement("div");
  host.innerHTML = renderToStaticMarkup(<WorkCost task={{ cost, task_budget_usd: cap, own_budget_usd: own }} />);
  return host;
};
const meter = (host: HTMLElement) => host.querySelector('[aria-label="任务费用上限使用比例"]');

describe("WorkCost", () => {
  it.each([null, undefined, { ...COST, runs: 0, cost_usd: 0, by_model: [] }])("keeps missing costs and no runs unavailable", cost => {
    const host = show(cost);
    expect(host.textContent).toContain("费用尚不可用");
    expect(host.textContent).not.toContain("$0.00");
    expect(meter(host)).toBeNull();
  });

  it("preserves a recorded zero and a priced total", () => {
    const zero = show({ ...COST, cost_usd: 0, by_model: [{ ...MODEL, cost_usd: 0 }] }, 10);
    expect(zero.textContent).toContain("$0.00 · 1 次运行");
    expect(meter(zero)?.getAttribute("aria-valuenow")).toBe("0");
    expect(show(COST).textContent).toContain("$2.00 · 1 次运行");
  });

  it.each([
    [0, 1, "按服务商价格计费"], [1, 0, "订阅等价费用"],
    [1, 1, "1 次按订阅等价计算，1 次按服务商价格计费"],
  ])("keeps the server accounting basis (%s, %s)", (equivalent_runs, provider_price_runs, label) => {
    expect(show({ ...COST, equivalent_runs, provider_price_runs }).textContent).toContain(label);
  });

  it.each([
    [1, 0, "1 次仍在运行"], [0, 1, "1 次费用未知（无结果或无定价，仅记录用量）"],
  ])("does not present unknown spend as zero (%s, %s)", (running, unpriced_runs, caveat) => {
    const host = show({ ...COST, cost_usd: 0, provider_price_runs: 0, running, unpriced_runs,
      by_model: [{ ...MODEL, cost_usd: null, running, unpriced_runs }] }, 10);
    expect(host.textContent).toContain("费用尚不可用");
    expect(host.textContent).toContain(caveat);
    expect(host.textContent).not.toContain("$0.00");
    expect(meter(host)).toBeNull();
  });

  it.each([0, 2])("labels a partial subtotal of %s without implying complete headroom", cost_usd => {
    const host = show({ ...COST, cost_usd, runs: 2, unpriced_runs: 1,
      by_model: [{ ...MODEL, cost_usd }, { ...MODEL, model: "unpriced-model", cost_usd: null, unpriced_runs: 1 }] }, 10);
    expect(host.textContent).toContain("已知费用小计");
    expect(host.textContent).toContain("1 次费用未知");
    expect(host.textContent).toContain("无法确定剩余额度");
    expect(meter(host)).toBeNull();
    expect(host.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it.each([0, 2])("keeps a running checkpoint of %s partial", cost_usd => {
    const host = show({ ...COST, cost_usd, running: 1, by_model: [{ ...MODEL, cost_usd, running: 1 }] }, 10);
    expect(host.textContent).toContain("已知费用小计");
    expect(host.textContent).toContain("1 次仍在运行");
    expect(meter(host)).toBeNull();
  });

  it("uses the server total once, including task Manager but excluding main Manager turns", () => {
    const cost: TaskCost = { ...COST, cost_usd: 3, runs: 2,
      by_role: { ...COST.by_role, task_manager: { cost_usd: 1, runs: 1 } },
      by_model: [MODEL, { ...MODEL, model: "task-manager-model", cost_usd: 1 }],
      manager_by_model: [{ ...MODEL, model: "main-manager-model", cost_usd: 99 }],
    };
    const host = show(cost, 10);
    expect(host.textContent).toContain("$3.00 · 2 次运行");
    expect(host.textContent).toContain("任务管家 · 1 次运行 · $1.00");
    expect(host.textContent).toContain("管家 · main-manager-model");
    expect(host.textContent).toContain("不计入任务费用或上限");
    expect(meter(host)?.getAttribute("aria-valuenow")).toBe("30");
    expect(show(COST).textContent).not.toContain("任务管家");
  });

  it("uses Chinese labels, model caveats and cache descriptions throughout the Work cost panel", () => {
    const host = show({ ...COST,
      by_role: { ...COST.by_role, explorer: { cost_usd: 0.1, runs: 1 }, consult: { cost_usd: 0.1, runs: 1 } },
      by_model: [
        { ...MODEL, model: null, running: 1, unpriced_runs: 1, interrupted_runs: 1, cache_reported_runs: 0, cache_missing_runs: 1 },
        { ...MODEL, cache_reported_runs: 1, cache_missing_runs: 1 },
      ],
    });
    for (const label of ["按智能体与模型统计费用", "规划者", "执行者", "评审者", "子智能体", "咨询", "按模型", "未记录",
      "1 次运行中，1 次费用未知，1 次已中断"]) expect(host.textContent).toContain(label);
    expect([...host.querySelectorAll("th")].map(cell => cell.textContent)).toEqual([
      "模型", "运行次数", "输入", "输出", "缓存读取", "缓存写入", "缓存命中", "费用",
    ]);
    expect(host.querySelector('[role="meter"]')?.getAttribute("aria-label")).toBe("规划者：占任务费用的比例");
    expect([...host.querySelectorAll("td[title]")].map(cell => cell.getAttribute("title"))).toEqual([
      "此运行时不报告缓存用量", "缓存读取 /（输入 + 缓存读取 + 缓存写入） · 数据不完整：2 次运行中有 1 次报告缓存用量",
    ]);
  });

  it("explains the effective cap and its source without dividing by zero", () => {
    expect(show(COST).textContent).toContain("未设置上限");
    const zero = show(COST, 0, 0);
    expect(zero.textContent).toContain("上限为0");
    expect(zero.textContent).toContain("本任务单独设置");
    expect(zero.innerHTML).not.toMatch(/NaN|Infinity/);
    expect(meter(zero)).toBeNull();
    const inherited = show(COST, 10);
    expect(inherited.textContent).toContain("沿用项目每任务上限");
    expect(inherited.textContent).toContain("$10.00");
    expect(show(COST, 10, 8).textContent).toContain("本任务单独设置");
    expect(meter(show(null, 10))).toBeNull();
  });

  it.each([[7.9, 79, ""], [8, 80, "接近上限"], [10, 100, "已达上限"]])(
    "reuses the cap thresholds at %s", (cost_usd, percent, warning) => {
      const host = show({ ...COST, cost_usd }, 10);
      expect(meter(host)?.getAttribute("aria-valuenow")).toBe(String(percent));
      expect(host.textContent).toContain(`已使用 $10.00 上限的 ${percent}%${warning ? ` · ${warning}` : ""}`);
      if (!warning) expect(host.textContent).not.toMatch(/接近上限|已达上限/);
    },
  );

  it("preserves the default spend meter language outside Work", () => {
    const html = renderToStaticMarkup(<TaskSpendMeter spent={8} cap={10} />);
    expect(html).toContain('aria-label="Share of the task spend cap used"');
    expect(html).toContain("80% of $10.00 cap · near the cap");
    expect(html).not.toContain("上限使用比例");
  });

  it("mounts read-only with no requests or editable controls", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const host = document.createElement("div");
    const root = createRoot(host);
    try {
      await act(async () => { root.render(<WorkCost task={{ cost: COST, task_budget_usd: 1, own_budget_usd: null }} />); });
      expect(host.querySelector("section")).not.toBeNull();
      expect(host.querySelector("button, input, select, form")).toBeNull();
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      act(() => root.unmount());
      vi.unstubAllGlobals();
    }
  });
});
