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
  let updates: { body: Record<string, unknown>; session: string }[];
  let confirms: { id: string; session: string }[];
  let updateMode: "confirm" | "ok" | "bad" | "down" | "denied";
  let hangGet: boolean;
  let getForbidden: boolean;
  let readbackFails: boolean;
  let confirmExpires: boolean;
  let gets: number;
  let lostResponse: "network" | "json" | "server" | null;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    unavailable = false;
    holds = []; decisions = []; needsConfirmation = false;
    updates = []; confirms = []; updateMode = "confirm";
    hangGet = false; getForbidden = false; readbackFails = false; confirmExpires = false; gets = 0; lostResponse = null;
    result = { mode: "off", daily_usd: null, daily_equivalent_usd: null, roles: {}, problem: null,
      counters: [], recent: [], recommended_daily_usd: null, recommended_daily_equivalent_usd: null };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const headers = (init?.headers ?? {}) as Record<string, string>;
      const session = headers["X-Looper-Session"] ?? "";
      if (url === "/api/v1/cost_holds/list") return Response.json({ status: "ok", result: { holds } });
      if (url === "/api/v1/cost_holds/decide") {
        decisions.push(JSON.parse(String(init?.body)));
        if (needsConfirmation) return Response.json({ status: "needs_confirmation", confirmation_id: "confirm-1",
          expires_in_seconds: 120, impact: "Raise the task cap to $4.00?" }, { status: 202 });
        holds = []; return Response.json({ status: "ok", result: { state: "stopped" } });
      }
      if (url === "/api/v1/confirm") {
        const body = JSON.parse(String(init?.body)) as { confirmation_id: string };
        confirms.push({ id: body.confirmation_id, session });
        if (body.confirmation_id === "confirm-guard") {
          if (confirmExpires) return Response.json({ status: "error", error: { code: "invalid", message: "确认已过期" } }, { status: 400 });
          applyUpdate(updates.at(-1)?.body ?? {});
          if (lostResponse) return loseResponse();
          return Response.json({ status: "ok", result });
        }
        holds = [];
        return Response.json({ status: "ok", result: { state: "continued" } });
      }
      if (url === "/api/v1/cost_guard/update") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        updates.push({ body, session });
        if (updateMode === "denied") return Response.json({ status: "error", error: { code: "forbidden", message: "仅操作者" } }, { status: 403 });
        if (updateMode === "bad") return Response.json({ status: "error", error: { code: "invalid", message: "金额不合法", field: "daily_usd" } }, { status: 400 });
        if (updateMode === "down") return Response.json({ status: "error", error: { code: "unavailable", message: "服务暂不可用" } }, { status: 503 });
        if (updateMode === "ok") {
          applyUpdate(body);
          if (lostResponse) return loseResponse();
          return Response.json({ status: "ok", result });
        }
        return Response.json({ status: "needs_confirmation", confirmation_id: "confirm-guard",
          expires_in_seconds: 120, impact: "Update cost guard settings?" }, { status: 202 });
      }
      expect(url).toBe("/api/v1/cost_guard/get");
      gets += 1;
      if (hangGet) return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
      if (getForbidden) return Response.json({ status: "error", error: { code: "forbidden", message: "forbidden" } }, { status: 403 });
      if (readbackFails && gets > 1) return Response.json({ status: "error", error: { code: "unavailable", message: "读回失败" } }, { status: 503 });
      return unavailable
        ? Response.json({ status: "error", error: { code: "unavailable", message: "观察数据暂不可用" } }, { status: 503 })
        : Response.json({ status: "ok", result });
    }));
  });

  function loseResponse(): Response {
    if (lostResponse === "network") throw new TypeError("Connection lost");
    if (lostResponse === "json") return new Response('{"status":');
    return Response.json({ status: "error", error: { code: "unavailable", message: "Response lost" } }, { status: 503 });
  }

  function applyUpdate(body: Record<string, unknown>) {
    if (body.roles && typeof body.roles === "object") {
      const roles = { ...(result.roles as Record<string, unknown>) };
      for (const [role, caps] of Object.entries(body.roles as Record<string, unknown>)) {
        roles[role] = { ...((roles[role] as Record<string, unknown> | undefined) ?? {}), ...(caps as Record<string, unknown>) };
      }
      result = { ...result, roles };
    }
    for (const key of ["mode", "daily_usd", "daily_equivalent_usd"] as const) {
      if (key in body) result = { ...result, [key]: body[key] };
    }
  }

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function render() {
    await act(async () => root.render(<CostGuardSettingsSection />));
  }

  it("审查预算等待不会声称停下任务，并说明提高上限不会重发动作", async () => {
    result = { ...result, mode: "enforce" };
    holds = [{ id: 8, scope: "role_day", scope_ref: "outbound_check", currency: "provider_priced_usd",
      spent_usd: 1, limit_usd: 1, task_ids: [], reason: "review_budget", created_at: "2026-01-15T00:00:00Z" }];
    await render();
    expect(container.textContent).toContain("24 小时未回答，上限不变");
    expect(container.textContent).toContain("被拦下的动作不会自动重发");
    expect(container.textContent).toContain("后台角色达到自身、定时任务或全局当天上限时停止");
    expect(container.textContent).toContain("定时触发到上限时跳过这次");
  });

  it("关联任务的全局等待仍会说明到期停下任务", async () => {
    holds = [{ id: 9, scope: "global_day", scope_ref: "global", currency: "provider_priced_usd",
      spent_usd: 1, limit_usd: 1, task_ids: ["task-synthetic"], created_at: "2026-01-15T00:00:00Z" }];
    await render();
    expect(container.textContent).toContain("24 小时未回答会停下任务");
    expect(container.textContent).not.toContain("24 小时未回答，上限不变");
  });

  it("does_not_write_on_read_or_cancel", async () => {
    await render();
    expect(isSettingsPage("cost")).toBe(true);
    expect(settingsPageLabel("cost")).toBe("花费");
    expect(container.textContent).toContain("已关闭");
    expect(container.textContent).toContain("未设上限");
    expect(container.textContent).toContain("先观察一周");
    expect(container.textContent).toContain("暂无观察记录");
    expect(container.querySelector("#cost-guard-mode")).not.toBeNull();
    for (const role of ["processor", "leads_check", "advice_check", "outbound_check", "advisor"]) {
      expect(container.querySelector(`#cost-guard-${role}-daily-usd`)).not.toBeNull();
    }
    expect(container.querySelector("#cost-guard-planner-daily-usd")).toBeNull();
    expect(updates).toEqual([]);
    await setMode("observe");
    expect((container.querySelector("#cost-guard-mode") as HTMLSelectElement).value).toBe("observe");
    await click("取消编辑");
    expect((container.querySelector("#cost-guard-mode") as HTMLSelectElement).value).toBe("off");
    expect(updates).toEqual([]);
    expect(container.textContent).not.toContain("已保存");
  });

  it("renders_a_partial_observation_without_hiding_the_form", async () => {
    result = { ...result, mode: "observe", daily_usd: 5,
      recent: [{ run_kind: "worker" }] };
    await render();
    expect(container.querySelector("#cost-guard-mode")).not.toBeNull();
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value).toBe("5");
    expect(container.textContent).toContain("worker");
    expect(container.textContent).not.toContain("超出上限");
    expect(container.textContent).not.toContain("真钱 未设上限");
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
    expect(container.querySelector("#cost-guard-mode")).not.toBeNull();
    expect(updates).toEqual([]);
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

  async function click(label: string) {
    await act(async () => {
      (Array.from(container.querySelectorAll("button")).find((button) => button.textContent === label) as HTMLButtonElement).click();
    });
  }

  async function setMode(value: string) {
    const select = container.querySelector("#cost-guard-mode") as HTMLSelectElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, value);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  async function setAmount(id: string, value: string) {
    const input = container.querySelector(`#${id}`) as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  it("validates_blank_zero_and_invalid_caps", async () => {
    result = { ...result, daily_usd: 5 };
    await render();
    await setAmount("cost-guard-daily-usd", " ");
    await click("保存");
    expect(updates.map((call) => call.body)).toEqual([{ daily_usd: null }]);
    expect(container.textContent).toContain("Update cost guard settings?");
    await click("取消确认");
    expect(confirms).toEqual([]);
    expect(container.textContent).not.toContain("Update cost guard settings?");
    await setAmount("cost-guard-daily-usd", "0");
    await click("保存");
    expect(updates.map((call) => call.body)).toEqual([{ daily_usd: null }, { daily_usd: 0 }]);
    await click("取消确认");
    const before = updates.length;
    await setAmount("cost-guard-daily-usd", "-1");
    expect(container.querySelector("#cost-guard-save")?.getAttribute("aria-disabled")).toBe("true");
    await click("保存");
    await setAmount("cost-guard-daily-usd", "Infinity");
    await click("保存");
    expect(updates).toHaveLength(before);
    await setAmount("cost-guard-daily-usd", "5");
    await setAmount("cost-guard-processor-daily-equivalent-usd", "4");
    await click("保存");
    expect(updates.at(-1)?.body).toEqual({ roles: { processor: { daily_equivalent_usd: 4 } } });
    expect(JSON.stringify(updates.at(-1)?.body)).not.toContain("unanswered_pushes");
  });

  it.each([1e-7, 1.2345678901234566e-7, 1e21, Number.MIN_VALUE, Number.MAX_VALUE])(
    "round trips cap %s as decimal text and keeps unedited global and role fields out of patches", async (cap) => {
      result = { ...result, daily_usd: cap, roles: { processor: { daily_equivalent_usd: cap } } };
      updateMode = "ok";
      await render();
      const decimal = (container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value;
      expect(decimal).toMatch(/^\d+(?:\.\d+)?$/);
      expect(Number(decimal)).toBe(cap);
      expect((container.querySelector("#cost-guard-processor-daily-equivalent-usd") as HTMLInputElement).value).toBe(decimal);
      await setMode("observe");
      await click("保存");
      expect(updates.map((call) => call.body)).toEqual([{ mode: "observe" }]);
      await setAmount("cost-guard-daily-usd", "0");
      await click("保存");
      await setAmount("cost-guard-daily-usd", decimal);
      await click("保存");
      expect(updates.map((call) => call.body)).toEqual([{ mode: "observe" }, { daily_usd: 0 }, { daily_usd: cap }]);
      expect(result.daily_usd).toBe(cap);
      expect(result.roles).toEqual({ processor: { daily_equivalent_usd: cap } });
    },
  );

  it("refuses a nonzero decimal that would silently underflow to zero", async () => {
    await render();
    await setAmount("cost-guard-processor-daily-usd", `0.${"0".repeat(324)}1`);
    await click("保存");
    expect(container.textContent).toContain("请填写非负的有限金额");
    expect(updates).toEqual([]);
  });

  it("saves_only_after_explicit_confirmation_and_reads_back", async () => {
    holds = [{ id: 3, scope: "task", scope_ref: "demo", currency: "provider_priced_usd", limit_usd: 1,
      spent_usd: 1, state: "pending", created_at: "2026-11-20T08:00:00Z" }];
    needsConfirmation = true;
    await render();
    await click("停");
    await setMode("observe");
    await setAmount("cost-guard-daily-usd", "5");
    await click("保存");
    expect(updates).toHaveLength(1);
    expect(updates[0].body).toEqual({ mode: "observe", daily_usd: 5 });
    expect(container.textContent).toContain("Update cost guard settings?");
    expect(container.textContent).toContain("确认停下");
    expect(container.textContent).not.toContain("已保存");
    await click("保存");
    expect(updates).toHaveLength(1);
    await click("确认停下");
    expect(confirms.map((call) => call.id)).toEqual(["confirm-1"]);
    await click("确认保存");
    expect(confirms.map((call) => call.id)).toEqual(["confirm-1", "confirm-guard"]);
    expect(confirms[1].session).toBe(updates[0].session);
    expect(confirms[0].session).not.toBe(confirms[1].session);
    expect((container.querySelector("#cost-guard-mode") as HTMLSelectElement).value).toBe("observe");
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value).toBe("5");
    expect(document.activeElement).toBe(container.querySelector("#cost-guard-save"));
    expect(container.textContent).not.toContain("已保存");
    expect(container.textContent).not.toContain("未写入");

    await act(async () => root.unmount());
    container.remove();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    updateMode = "ok";
    confirms = []; updates = [];
    result = { mode: "off", daily_usd: null, daily_equivalent_usd: null, roles: {}, problem: null,
      counters: [], recent: [], recommended_daily_usd: null, recommended_daily_equivalent_usd: null };
    await render();
    await setAmount("cost-guard-daily-equivalent-usd", "1");
    await click("保存");
    expect(confirms).toEqual([]);
    expect(updates.map((call) => call.body)).toEqual([{ daily_equivalent_usd: 1 }]);
    expect((container.querySelector("#cost-guard-daily-equivalent-usd") as HTMLInputElement).value).toBe("1");
  });

  it("keeps_draft_on_write_or_readback_failure", async () => {
    await render();
    updateMode = "bad";
    await setAmount("cost-guard-daily-usd", "4");
    await click("保存");
    expect(container.textContent).toContain("daily_usd: 金额不合法");
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value).toBe("4");
    updateMode = "down";
    await click("保存");
    expect(container.textContent).toContain("服务暂不可用");
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value).toBe("4");
    updateMode = "ok";
    await click("重新读取");
    await setAmount("cost-guard-daily-usd", "4");
    readbackFails = true;
    await click("保存");
    expect(container.textContent).toContain("状态未知。请只读重试，不会自动再次保存。");
    expect(container.textContent).not.toContain("已保存");
    expect(container.textContent).not.toContain("未写入");
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value).toBe("4");
    const written = updates.length;
    await click("重新读取");
    expect(updates).toHaveLength(written);
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value).toBe("4");

    await act(async () => root.unmount());
    container.remove();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    updateMode = "confirm";
    readbackFails = false;
    updates = [];
    result = { mode: "off", daily_usd: null, daily_equivalent_usd: null, roles: {}, problem: null,
      counters: [], recent: [], recommended_daily_usd: null, recommended_daily_equivalent_usd: null };
    await render();
    await setMode("enforce");
    await click("保存");
    expect(container.textContent).toContain("Update cost guard settings?");
    confirmExpires = true;
    await click("确认保存");
    expect(container.textContent).toContain("确认已过期");
    expect(container.textContent).not.toContain("Update cost guard settings?");
    await click("保存");
    expect(updates).toHaveLength(2);
    expect(updates[1].body).toEqual({ mode: "enforce" });
  });

  it.each([
    ["direct", "network"], ["direct", "json"], ["direct", "server"],
    ["confirm", "network"], ["confirm", "json"], ["confirm", "server"],
  ] as const)("locks writes after a committed %s save loses its %s response until a read succeeds", async (action, failure) => {
    updateMode = action === "direct" ? "ok" : "confirm";
    await render();
    await setMode("observe");
    await setAmount("cost-guard-daily-usd", "4");
    lostResponse = failure;
    await click("保存");
    if (action === "confirm") await click("确认保存");
    expect(result.daily_usd).toBe(4); // The server committed before the response failed.
    expect(container.textContent).toContain("状态未知");
    expect(container.textContent).not.toContain("已保存");
    expect(container.textContent).not.toContain("Update cost guard settings?");
    expect((container.querySelector("#cost-guard-mode") as HTMLSelectElement).disabled).toBe(true);
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).disabled).toBe(true);
    expect((Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "取消编辑") as HTMLButtonElement).disabled).toBe(true);
    await click("保存");
    expect(updates).toHaveLength(1);
    expect(confirms).toHaveLength(action === "confirm" ? 1 : 0);
    readbackFails = true;
    await click("重新读取");
    expect(container.textContent).toContain("状态未知");
    expect((container.querySelector("#cost-guard-mode") as HTMLSelectElement).disabled).toBe(true);
    readbackFails = false;
    result = { ...result, daily_usd: 7 }; // Reconcile with the current server state, not the old draft.
    await click("重新读取");
    expect(container.textContent).not.toContain("状态未知");
    expect((container.querySelector("#cost-guard-daily-usd") as HTMLInputElement).value).toBe("7");
    expect((container.querySelector("#cost-guard-mode") as HTMLSelectElement).disabled).toBe(false);
    expect(container.querySelector("#cost-guard-save")?.getAttribute("aria-disabled")).toBe("true");
    expect(updates).toHaveLength(1);
    expect(confirms).toHaveLength(action === "confirm" ? 1 : 0);
  });

  it("shows_loading_forbidden_and_retry_without_writing", async () => {
    hangGet = true;
    await render();
    expect(container.textContent).toContain("正在读取花费设置");
    expect(container.querySelector("#cost-guard-mode")).toBeNull();
    expect(updates).toEqual([]);
    await act(async () => root.unmount());
    container.remove();

    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    hangGet = false;
    getForbidden = true;
    await render();
    expect(container.textContent).toContain("仅操作者可查看和修改花费设置");
    expect(container.querySelector("#cost-guard-save")).toBeNull();
    expect(container.querySelector("input,select")).toBeNull();
    expect(container.textContent).not.toContain("确认保存");
    expect(updates).toEqual([]);
    await act(async () => root.unmount());
    container.remove();

    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    getForbidden = false;
    unavailable = true;
    await render();
    expect(container.textContent).toContain("观察数据暂不可用");
    expect(container.textContent).not.toContain("暂无观察记录");
    await click("重新读取");
    expect(updates).toEqual([]);
  });

});
