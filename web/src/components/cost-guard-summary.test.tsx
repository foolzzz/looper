// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CostGuardSummary } from "@/components/cost-guard-summary";
import { fetchCostGuard, fetchCostHolds, type CostGuardCounter, type CostGuardView } from "@/lib/api-cost-guard";
import { V1Error } from "@/lib/v1";

vi.mock("@/lib/api-cost-guard", () => ({ fetchCostGuard: vi.fn(), fetchCostHolds: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const counter = (currency: string, spent: number, reserved: number, limit: number | null, status: "known" | "unknown" = "known"): CostGuardCounter => ({
  scope: "global_day", scope_ref: "global", currency, spent_usd: spent, reserved_usd: reserved, limit_usd: limit,
  cost_status: status, unknown_models: status === "unknown" ? ["missing-price"] : [],
});

function view(patch: Partial<CostGuardView> = {}): CostGuardView {
  return {
    mode: "observe", daily_usd: 5, daily_equivalent_usd: 9, roles: {},
    counters: [counter("provider_priced_usd", 1, 2, 5), counter("equivalent_usd", 3, 1, 9),
      { ...counter("provider_priced_usd", 99, 99, 1), scope: "role_day", scope_ref: "processor" },
      { ...counter("equivalent_usd", 99, 0, 1), scope: "task", scope_ref: "task-synthetic" }],
    recent: [], recommended_daily_usd: null, recommended_daily_equivalent_usd: null, problem: null, ...patch,
  };
}

describe("花费守卫余量", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    vi.mocked(fetchCostGuard).mockReset();
    vi.mocked(fetchCostHolds).mockReset();
    vi.mocked(fetchCostHolds).mockResolvedValue({ holds: [] });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  async function show() {
    await act(async () => root.unmount());
    container.remove();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<CostGuardSummary />));
  }

  it("subtracts_reservations_in_each_global_currency", async () => {
    vi.mocked(fetchCostGuard).mockResolvedValue(view());
    await show();
    const text = container.textContent ?? "";
    expect(text).toContain("真钱观察余量：剩余 $2.00");
    expect(text).toContain("等价花费观察余量：剩余 $5.00");
    expect(text).not.toContain("$99");
    expect(text).not.toContain("供应商账单余额");
    expect(container.querySelector('a[href="#settings/cost"]')?.textContent).toBe("没有等待决定的花费");
  });

  it("renders_zero_overage_and_unset_without_merging_currencies", async () => {
    vi.mocked(fetchCostGuard).mockResolvedValue(view({
      mode: "enforce", daily_usd: 2, daily_equivalent_usd: 1,
      counters: [counter("provider_priced_usd", 1, 1, 2), counter("equivalent_usd", 2, 1, 1)],
    }));
    await show();
    expect(container.textContent).toContain("真钱准入余量：剩余 $0.00");
    expect(container.textContent).toContain("等价花费准入余量：超出 $2.00");
    expect(container.textContent).not.toContain("剩余 $3.00");
    vi.mocked(fetchCostGuard).mockResolvedValue(view({
      daily_usd: null, daily_equivalent_usd: 4,
      counters: [counter("provider_priced_usd", 1, 1, null), counter("equivalent_usd", 1, 1, 4)],
    }));
    await show();
    expect(container.textContent).toContain("真钱：未设上限");
    expect(container.textContent).toContain("等价花费观察余量：剩余 $2.00");
    expect(container.textContent).not.toContain("真钱观察余量");
    vi.mocked(fetchCostGuard).mockResolvedValue(view({ mode: "off" }));
    await show();
    expect(container.textContent).toContain("守卫已关闭");
    expect(container.textContent).not.toContain("剩余 $2.00");
    expect(container.textContent).not.toContain("剩余 $5.00");
  });

  it("does_not_invent_headroom_for_missing_or_unknown_counters", async () => {
    vi.mocked(fetchCostGuard).mockResolvedValue(view({ counters: [counter("equivalent_usd", 3, 1, 9)] }));
    await show();
    expect(container.textContent).toContain("真钱：暂无守卫计数");
    expect(container.textContent).toContain("等价花费观察余量：剩余 $5.00");
    expect(container.textContent).not.toContain("真钱观察余量");

    vi.mocked(fetchCostGuard).mockResolvedValue(view({
      counters: [counter("provider_priced_usd", 1, 2, 5, "unknown"), counter("equivalent_usd", 3, 1, 9)],
    }));
    await show();
    expect(container.textContent).toContain("真钱：花费不完整/余量暂不可确定");
    expect(container.textContent).not.toContain("真钱观察余量");
    expect(container.textContent).toContain("等价花费观察余量：剩余 $5.00");

    vi.mocked(fetchCostGuard).mockResolvedValue(view({ problem: "Stored cost guard settings are invalid; the guard is off." }));
    await show();
    expect(container.textContent).toContain("花费不完整/余量暂不可确定 Stored cost guard settings are invalid; the guard is off.");
    expect(container.textContent).not.toContain("剩余 $");
    expect(container.textContent).not.toContain("超出 $");

    vi.mocked(fetchCostGuard).mockResolvedValue(view({ mode: "off" }));
    await show();
    expect(container.textContent).toContain("守卫已关闭");
    expect(container.textContent).not.toContain("剩余 $2.00");
    expect(container.textContent).not.toContain("剩余 $5.00");
  });

  it("isolates_loading_forbidden_errors_and_pending_count", async () => {
    vi.mocked(fetchCostGuard).mockImplementation((signal?: AbortSignal) => new Promise((_resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    vi.mocked(fetchCostHolds).mockImplementation((signal?: AbortSignal) => new Promise((_resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    await show();
    expect(container.textContent).toContain("正在读取花费守卫");
    expect(container.textContent).toContain("正在读取等待决定的花费");
    expect(container.textContent).not.toContain("没有等待决定的花费");
    expect(container.textContent).not.toContain("剩余 $0.00");

    vi.mocked(fetchCostGuard).mockRejectedValue(new V1Error(403, "forbidden", "forbidden"));
    vi.mocked(fetchCostHolds).mockResolvedValue({ holds: [] });
    await show();
    expect(container.textContent).toContain("仅操作者可查看花费余量");
    expect(container.textContent).not.toContain("剩余 $");

    vi.mocked(fetchCostGuard).mockRejectedValue(new Error("守卫读取失败"));
    vi.mocked(fetchCostHolds).mockRejectedValue(new Error("list down"));
    await show();
    expect(container.textContent).toContain("守卫读取失败");
    expect(container.textContent).toContain("等待数量暂不可用");
    expect(container.textContent).not.toContain("没有等待决定的花费");
    expect(container.textContent).not.toContain("剩余 $0.00");

    vi.mocked(fetchCostGuard).mockResolvedValue(view());
    vi.mocked(fetchCostHolds).mockResolvedValue({ holds: [{ id: 1 }, { id: 2 }] as never });
    await show();
    expect(container.textContent).toContain("真钱观察余量：剩余 $2.00");
    expect(container.querySelector('a[href="#settings/cost"]')?.textContent).toBe("2 条等待决定的花费");
    vi.mocked(fetchCostGuard).mockRejectedValue(new Error("刷新失败"));
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(container.textContent).toContain("余量已过期");
    expect(container.textContent).toContain("已过期 · 真钱观察余量：剩余 $2.00");
    expect(container.textContent).toContain("刷新失败");

    let late: (() => void) | undefined;
    vi.mocked(fetchCostGuard).mockImplementation((signal?: AbortSignal) => new Promise((resolve) => {
      late = () => resolve(view({ mode: "enforce", daily_usd: 1, counters: [counter("provider_priced_usd", 0, 0, 1)] }));
      signal?.addEventListener("abort", () => undefined);
    }));
    vi.mocked(fetchCostHolds).mockResolvedValue({ holds: [] });
    await show();
    await act(async () => root.unmount());
    await act(async () => { late?.(); });
    expect(container.textContent ?? "").not.toContain("准入余量");
  });
});
