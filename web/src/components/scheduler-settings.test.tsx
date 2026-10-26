// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SchedulerSection } from "@/components/scheduler-settings";
import { ScheduleDetail } from "@/components/schedule-detail";
import type { ScheduleInfo } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const view = (min: number, source: string, timeout: number | null = null) => ({
  min_interval_seconds: { value: min, source, built_in: 300, minimum: 1, gated_value: Math.min(60, min) },
  run_timeout_seconds: { value: timeout, source: "built-in", built_in: null, minimum: 1, maximum: 2592000 },
});
const waits = (max = 604800, min = 300, source = "built-in") => ({
  max_wait_seconds: { value: max, default: 604800, min: 60, max: 2592000, source },
  min_check_seconds: { value: min, default: 300, min: 10, max: 86400, source },
});

describe("Settings → Scheduler", () => {
  let container: HTMLDivElement;
  let root: Root;
  let puts: unknown[];

  beforeEach(() => {
    puts = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (String(_url) === "/api/v1/wait_settings/get") return Response.json({ status: "ok", result: waits() });
      if (String(_url) === "/api/v1/wait_settings/update") {
        const body = JSON.parse(String(init?.body));
        puts.push(body);
        if (body.min_check_seconds === 0) return Response.json({ status: "error", error: { code: "invalid", message: "min_check_seconds must be at least 10" } }, { status: 400 });
        return Response.json({ status: "ok", result: waits(body.max_wait_seconds ?? 604800, body.min_check_seconds ?? 300, "setting") });
      }
      if (String(_url) === "/api/v1/scheduler_settings/update") {
        const body = JSON.parse(String(init?.body));
        puts.push(body);
        if (body.min_interval_seconds === 0) return Response.json({ status: "error", error: { code: "invalid", message: "bad value" } }, { status: 400 });
        return Response.json({ status: "ok", result: view(body.min_interval_seconds ?? 120, "setting") });
      }
      if (String(_url) === "/api/v1/scheduler_settings/get") return Response.json({ status: "ok", result: view(120, "env") });
      throw new Error(`Unexpected request: ${_url}`);
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const typeInto = (input: HTMLInputElement, text: string) =>
    act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });

  it("shows each effective value with its source, saves one value and shows a refusal", async () => {
    await act(async () => root.render(<SchedulerSection />));
    await flush();
    const inputs = container.querySelectorAll("input");
    expect((inputs[0] as HTMLInputElement).value).toBe("120");
    expect(container.textContent).toContain("deprecated LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS");
    expect(container.textContent).toContain("Built-in value");
    const save = container.querySelector("button[type=submit]") as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    await typeInto(inputs[0] as HTMLInputElement, "90");
    expect(save.disabled).toBe(false);
    await act(async () => { save.click(); });
    await flush();
    expect(puts).toEqual([{ min_interval_seconds: 90 }]);
    expect(container.textContent).toContain("Saved here");
    expect(container.textContent).toContain("Use the built-in (300 s)");

    await typeInto(container.querySelectorAll("input")[0] as HTMLInputElement, "0");
    await act(async () => { (container.querySelector("button[type=submit]") as HTMLButtonElement).click(); });
    await flush();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("bad value");
  });

  it("keeps what is typed in the other field when one is saved", async () => {
    await act(async () => root.render(<SchedulerSection />));
    await flush();
    await typeInto(container.querySelectorAll("input")[1] as HTMLInputElement, "45");
    await typeInto(container.querySelectorAll("input")[0] as HTMLInputElement, "90");
    await act(async () => { (container.querySelector("button[type=submit]:not(:disabled)") as HTMLButtonElement).click(); });
    await flush();
    expect(puts).toEqual([{ min_interval_seconds: 90 }]);
    const inputs = container.querySelectorAll("input");
    expect((inputs[0] as HTMLInputElement).value).toBe("90");
    expect((inputs[1] as HTMLInputElement).value).toBe("45");
  });

  it.each([true, false])("shows a schedule's registered model summary or project inheritance (override: %s)", async (override) => {
    const schedule: ScheduleInfo = {
      manager_id: "manager", manager_name: "Manager",
      schedule_id: "schedule", project_id: "project", project_name: "Project", name: "Scheduled task",
      cron: "0 9 * * *", timezone: "UTC", description: "Every morning", requirement: "Check changes", work_dir: "",
      overlap: "skip", max_parallel_runs: 1, catch_up: "skip", run_timeout_seconds: null,
      run_timeout_effective_seconds: null, run_budget_usd: null, profile_overrides: override ? { worker: "p" } : {},
      task_limits: { worker: null, reviewer: null }, enabled: false, next_fire_at: null, last_fired_at: null,
      last_success_at: null, consecutive_failures: 0, created_via: "dashboard", created_at: "", updated_at: "",
      last_run: null, gate_command: null, gate_timeout_seconds: null, daily_budget_usd: null, notify: "never",
      notify_target: null, spent_today_usd: 0, alerts: { failures_since: null, stale_since: null },
      planner_session: { active: false, runtime: null, profile: null, last_task_id: null, last_task_title: null, updated_at: null },
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/registry/view") return Response.json({ status: "ok", result: { profiles: [{
        profile_id: "p", name: "Schedule profile", effort: "high", runtime_name: "Runtime",
        provider: { provider_id: "v", name: "Schedule account", auth: "api_key", vendor_type: "custom" },
        model: { model_id: "m", provider_id: "v", name: "Exact model", protocol: "openai-responses" },
      }] } });
      if (url === "/api/v1/schedule_admin/get") return Response.json({ status: "ok", result: { schedule } });
      if (url === "/api/v1/schedule_admin/runs") return Response.json({ status: "ok", result: {
        runs: [], next_before: null, routine_skips: { skipped_gate: 0, skipped_overlap: 0 },
      } });
      throw new Error(`Unexpected request: ${url}`);
    }));
    await act(async () => root.render(<ScheduleDetail scheduleId="schedule" onDeleted={() => {}} />));
    expect(container.textContent).toContain(override
      ? "Schedule profile · high — Runtime · Schedule account / Exact model / openai-responses"
      : "As the project sets them");
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("shows wait defaults and sources, saves seconds, resets to default and shows a refusal", async () => {
    await act(async () => root.render(<SchedulerSection />));
    await flush();
    const max = container.querySelector('input[id$="-max_wait_seconds"]') as HTMLInputElement;
    const min = container.querySelector('input[id$="-min_check_seconds"]') as HTMLInputElement;
    expect(max.value).toBe("604800");
    expect(min.value).toBe("300");
    expect(max.closest("form")?.textContent).toContain("Built-in value. Built-in default: 7 d");
    await typeInto(min, "600");
    await typeInto(max, "86400");
    await act(async () => { (max.closest("form")!.querySelector("button[type=submit]") as HTMLButtonElement).click(); });
    await flush();
    expect(puts).toEqual([{ max_wait_seconds: 86400 }]);
    expect(min.value).toBe("600");
    expect(max.closest("form")?.textContent).toContain("Saved here");
    await act(async () => { (max.closest("form")!.querySelector("button[type=button]") as HTMLButtonElement).click(); });
    await flush();
    expect(puts[1]).toEqual({ max_wait_seconds: null });
    await typeInto(min, "0");
    await act(async () => { (min.closest("form")!.querySelector("button[type=submit]") as HTMLButtonElement).click(); });
    await flush();
    expect(puts[2]).toEqual({ min_check_seconds: 0 });
    expect(container.querySelector("[role=alert]")?.textContent).toContain("min_check_seconds must be at least 10");
  });
});
