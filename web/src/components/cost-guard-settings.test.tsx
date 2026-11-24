// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CostGuardSettingsSection } from "@/components/cost-guard-settings";
import { isSettingsPage, settingsPageLabel } from "@/lib/settings-pages";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("花费观察", () => {
  let container: HTMLDivElement;
  let root: Root;
  let result: Record<string, unknown>;
  let unavailable: boolean;
  let holds: Record<string, unknown>[];
  let decisions: Record<string, unknown>[];
  let needsConfirmation: boolean;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    unavailable = false;
    holds = []; decisions = []; needsConfirmation = false;
    result = { mode: "off", daily_usd: null, daily_equivalent_usd: null, roles: {}, problem: null,
      counters: [], recent: [], recommended_daily_usd: null, recommended_daily_equivalent_usd: null };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/v1/cost_holds/list") return Response.json({ status: "ok", result: { holds } });
      if (String(input) === "/api/v1/cost_holds/decide") {
        decisions.push(JSON.parse(String(init?.body)));
        if (needsConfirmation) return Response.json({ status: "needs_confirmation", confirmation_id: "confirm-1",
          expires_in_seconds: 120, impact: "Raise the task cap to $4.00?" }, { status: 202 });
        holds = []; return Response.json({ status: "ok", result: { state: "stopped" } });
      }
      if (String(input) === "/api/v1/confirm") {
        holds = []; return Response.json({ status: "ok", result: { state: "continued" } });
      }
      expect(String(input)).toBe("/api/v1/cost_guard/get");
      return unavailable
        ? Response.json({ status: "error", error: { code: "unavailable", message: "观察数据暂不可用" } }, { status: 503 })
        : Response.json({ status: "ok", result });
    }));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function render() {
    await act(async () => root.render(<CostGuardSettingsSection />));
  }

  it("默认关闭、未设上限，并且没有编辑控件", async () => {
    await render();
    expect(isSettingsPage("cost")).toBe(true);
    expect(settingsPageLabel("cost")).toBe("花费");
    expect(container.textContent).toContain("已关闭");
    expect(container.textContent).toContain("未设上限");
    expect(container.textContent).toContain("先观察一周");
    expect(container.querySelectorAll("input,select,button,textarea")).toHaveLength(0);
    expect(container.textContent).toContain("暂无观察记录");
  });

  it("分钱种展示已花和预留，并列出本来会被拦下的记录", async () => {
    result = { ...result, mode: "observe", daily_usd: 2, recommended_daily_usd: 3,
      counters: [{ scope: "task", scope_ref: "demo", currency: "provider_priced_usd", spent_usd: 0.25,
        reserved_usd: 2, limit_usd: 2 }],
      recent: [{ run_kind: "worker", currency: "provider_priced_usd", estimate_usd: 1,
        estimate_source: "run_cap", decision: "ask", scope: "task", scope_ref: "demo", action: "ask" }] };
    await render();
    expect(container.textContent).toContain("仅观察");
    expect(container.textContent).toContain("$0.25");
    expect(container.textContent).toContain("$2.00");
    expect(container.textContent).toContain("worker");
    expect(container.textContent).toContain("需询问");
    expect(container.textContent).toContain("$3.00");
  });

  it("执行中列出当前暂缓，并且仍然没有编辑控件", async () => {
    result = { ...result, mode: "enforce", holds: [
      { provider: "prov-synthetic", reason: "rejected", until: "2026-01-15T21:00:00+00:00", window: "five_hour",
        reset_known: true },
      { provider: "prov-cooldown", reason: "quota_cooldown", until: "2026-01-15T20:10:00+00:00", window: null,
        reset_known: false }] };
    await render();
    expect(container.textContent).toContain("执行中");
    expect(container.textContent).toContain("用量窗口暂缓");
    expect(container.textContent).toContain("账号被拒");
    expect(container.textContent).toContain("配额冷却");
    expect(container.textContent).toContain("prov-synthetic");
    expect(container.textContent).toContain("2026-01-15T21:00:00+00:00");
    expect(container.textContent).toContain("2026-01-15T20:10:00+00:00");
    expect(container.textContent).toContain("重置时间未知");
    expect(container.querySelectorAll("input,select,button,textarea")).toHaveLength(0);
  });

  it("暂缓显示到重置时刻，观察记录带上本来会暂缓到的时间和说明", async () => {
    const until = "2026-01-15T21:00:00+00:00";
    result = { ...result, mode: "enforce", recent: [
      { run_kind: "schedule", currency: "equivalent_usd", estimate_usd: 0.1, estimate_source: "floor",
        decision: "hold", scope: "schedule_day", scope_ref: "sched", action: null, would_hold_until: until,
        note: `hold until ${until}` },
      { run_kind: "schedule", currency: "equivalent_usd", estimate_usd: 0.1, estimate_source: "floor",
        decision: "admit", scope: "schedule_day", scope_ref: "sched", action: null, would_hold_until: until,
        note: `would hold until ${until}` },
    ] };
    await render();
    expect(container.textContent).toContain(`暂缓到 ${until}`);
    expect(container.textContent).toContain(`本来会放行 · ${until} · would hold until ${until}`);
    expect(container.textContent).not.toContain("超出上限");
  });

  it("读取失败展示错误而不是成功或空数据", async () => {
    unavailable = true;
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("观察数据暂不可用");
    expect(container.textContent).not.toContain("暂无观察记录");
  });
  it("shows enforce holds and requires an explicit confirmation before raising the cap", async () => {
    result = { ...result, mode: "enforce" };
    holds = [{ id: 1, scope: "task", scope_ref: "demo", currency: "provider_priced_usd", limit_usd: 2,
      spent_usd: 2, state: "pending", created_at: "2026-11-20T08:00:00Z" }];
    needsConfirmation = true;
    await render();
    expect(container.textContent).toContain("执行中");
    expect(container.textContent).toContain("金额上限拒绝优先于用量窗口暂缓");
    expect(container.textContent).toContain("包括你自己发起的运行都会暂缓");
    expect(container.textContent).toContain("95% 利用率只暂缓后台角色和定时触发");
    expect(container.textContent).not.toContain("你自己发起的运行只记录");
    expect(container.textContent).toContain("C-1");
    expect(container.textContent).not.toContain("不拦截、不提问、不发消息");
    const amount = container.querySelector('input[aria-label="C-1 新上限（美元）"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(amount, "4");
      amount.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const click = async (label: string) => act(async () => {
      (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === label) as HTMLButtonElement).click();
    });
    await click("继续");
    expect(decisions).toEqual([{ hold_id: 1, decision: "continue", new_limit_usd: 4 }]);
    expect(container.textContent).toContain("Raise the task cap to $4.00?");
    expect(container.textContent).toContain("C-1");
    await click("确认提高上限");
    expect(container.textContent).toContain("没有等待决定的花费");
  });

  it.each(["enforce", "observe", "off"])("can stop a pending hold in %s without raising its cap", async (mode) => {
    result = { ...result, mode };
    holds = [{ id: 2, scope: "global_day", scope_ref: "today", currency: "equivalent_usd", limit_usd: 1,
      spent_usd: 1.5, state: "pending", created_at: "2026-11-20T08:00:00Z" }];
    needsConfirmation = true;
    await render();
    await act(async () => (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "停") as HTMLButtonElement).click());
    expect(container.textContent).toContain("确认停下");
    await act(async () => (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "确认停下") as HTMLButtonElement).click());
    expect(decisions).toEqual([{ hold_id: 2, decision: "stop" }]);
    expect(container.textContent).toContain("没有等待决定的花费");
  });

  it("continues an unanswered hold without an amount field", async () => {
    holds = [{ id: 4, scope: "schedule", scope_ref: "sched-brief", currency: null, limit_usd: null, spent_usd: null,
      pushes: 15, reason: "unanswered", state: "pending", created_at: "2026-11-20T08:00:00Z" }];
    needsConfirmation = true;
    await render();
    expect(container.querySelector('input[aria-label="C-4 新上限（美元）"]')).toBeNull();
    expect(container.textContent).toContain("15 条推送没人回");
    expect(container.textContent).not.toContain("新上限");
    await act(async () => (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "继续") as HTMLButtonElement).click());
    expect(decisions).toEqual([{ hold_id: 4, decision: "continue" }]);
    expect(container.textContent).toContain("确认继续");
    expect(container.textContent).not.toContain("确认提高上限");
  });

});
