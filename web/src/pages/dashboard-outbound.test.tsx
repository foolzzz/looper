// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPage } from "@/pages/dashboard";
import { fetchOverview, type Overview } from "@/lib/api";
import { fetchManagerMetrics, type ManagerMetrics, type OutboundChannelMetrics } from "@/lib/manager-metrics";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(), fetchOverview: vi.fn(),
}));
vi.mock("@/lib/manager-metrics", () => ({ fetchManagerMetrics: vi.fn() }));
vi.mock("@/lib/use-waiting-gates", () => ({ useWaitingGates: () => null }));
vi.mock("@/components/dashboard-gates", () => ({ DashboardGates: () => null }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const channel = {
  channel_id: "lark-demo", name: "演示渠道", queued: 3, backoffs_24h: 4, breaker_opens_24h: 1,
  failed_24h: { rejected: 2, transient: 1 }, uncertain_24h: 2, breaker_state: "closed" as const,
};
const base = {
  enabled: true, window_days: 7, turns: { by_delivery: {} },
  ack: { p50_ms: null, p95_ms: null, by_chat: {} }, first_reply: { p50_ms: null, p95_ms: null, by_chat: {} },
  empty: { count: 0, ratio: null }, proactive: { per_day: [], reply_rate: { all: null, by_source: {} } },
  outbound: [channel], outbound_started_at: "2026-11-22T10:00:00Z", outbound_since_start: true,
};

describe("Dashboard outbound figures", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchOverview).mockResolvedValue({
      tasks: { other_statuses: {} }, agents: { manager: null, planners: [], workers: [], reviewers: [] },
      projects: [], idle_projects: 0, health: { unhealthy: [] },
      schedules: { total: 0, enabled: 0, next: [], failing: [], skipped_today: {} },
      spend_today: { since: "", provider_priced_usd: 0, equivalent_usd: 0, runs: 0, running: 0, unpriced_runs: 0 },
      processes: [], tpm: { total: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, window_seconds: 60 },
    } as unknown as Overview);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });
  async function show(metrics: ManagerMetrics = base) {
    vi.mocked(fetchManagerMetrics).mockResolvedValue(metrics);
    await act(async () => root.render(<DashboardPage />));
    return container.querySelector('[aria-label="出站"]')!;
  }

  it("renders channel counts below the tiles and explains the shorter in-memory window", async () => {
    const outbound = await show();
    expect(outbound).not.toBeNull();
    for (const part of ["演示渠道", "排队 3", "退避 4", "熔断 1", "失败 3", "不确定 2", "未熔断"]) {
      expect(outbound.textContent).toContain(part);
    }
    expect(outbound.textContent).toContain("自 ");
    expect(outbound.textContent).toContain("退避、熔断");
    expect(outbound.querySelector("time")?.dateTime).toBe(base.outbound_started_at);
    const group = container.querySelector('section[aria-label="Manager"]')!;
    expect(group.innerHTML.indexOf("Proactive messages</p>")).toBeLessThan(group.innerHTML.indexOf('aria-label="出站"'));
    expect(group.innerHTML.indexOf('aria-label="出站"')).toBeLessThan(group.innerHTML.indexOf("<table"));
  });

  it.each<{
    breaker_state: OutboundChannelMetrics["breaker_state"];
    failed_24h: Record<string, number>;
    attention: boolean;
    state: string;
  }>([
    { breaker_state: "open" as const, failed_24h: {}, attention: true, state: "已熔断" },
    { breaker_state: "closed" as const, failed_24h: { rejected: 1 }, attention: true, state: "未熔断" },
    { breaker_state: "half_open" as const, failed_24h: {}, attention: false, state: "探测中" },
    { breaker_state: null, failed_24h: {}, attention: false, state: "未运行" },
  ])("marks attention for $breaker_state with $failed_24h", async ({ attention, state, ...changes }) => {
    const outbound = await show({ ...base, outbound: [{ ...channel, ...changes }] });
    const row = outbound.querySelector("li")!;
    expect(row.classList.contains("border-warning/50")).toBe(attention);
    expect(row.textContent).toContain(state);
  });

  it("keeps outbound figures when turn metrics are disabled", async () => {
    const outbound = await show({ ...base, enabled: false, outbound_since_start: false });
    expect(outbound.textContent).toContain("排队 3");
    expect(outbound.textContent).not.toContain("自 ");
    expect(container.textContent).toContain("Metrics are off");
  });

  it("shows a muted off line with a direct link to Chat settings", async () => {
    const outbound = await show({ ...base, outbound: { enabled: false } });
    expect(outbound.textContent).toContain("出站网关未启用");
    expect(outbound.querySelector('a[href="#settings/chat"]')).not.toBeNull();
    expect(outbound.querySelector("li")).toBeNull();
  });

  it("names the empty channel state without implying delivery success", async () => {
    const outbound = await show({ ...base, outbound: [] });
    expect(outbound.textContent).toContain("暂无出站渠道");
    expect(outbound.querySelector("li")).toBeNull();
  });
});
