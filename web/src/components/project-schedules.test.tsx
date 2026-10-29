// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlannerSessionStatus, ProjectSchedulesSection, ResetPlannerSession } from "@/components/project-schedules";
import type { Project, ScheduleInfo } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const emptySession: ScheduleInfo["planner_session"] = {
  active: false, runtime: null, profile: null, last_task_id: null, last_task_title: null, updated_at: null,
};
const schedule = (active = true): ScheduleInfo => ({
  schedule_id: "sched/a", name: "Daily check", timezone: "America/Los_Angeles", overlap: "parallel",
  planner_session: active ? {
    active: true, runtime: "claude-code", profile: "profile-a", last_task_id: "task-a",
    last_task_title: "Daily check · previous run", updated_at: "2026-10-19T01:00:00Z",
  } : emptySession,
} as ScheduleInfo);

describe("schedule Planner session", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("shows stored session status even for parallel, in the schedule's timezone, with its last task", async () => {
    await act(async () => root.render(<PlannerSessionStatus schedule={schedule()} />));
    expect(container.textContent).toContain("Planner session: continuing since");
    expect(container.textContent).toContain("Oct 18");
    expect(container.textContent).toContain("PDT");
    expect(container.textContent).toContain("last used by run Daily check · previous run");
    expect(container.querySelector("a")?.getAttribute("href")).toBe("#tasks/task-a");
    expect(container.textContent).not.toContain("profile-a");
  });

  it("shows none yet for an empty session", async () => {
    await act(async () => root.render(<PlannerSessionStatus schedule={schedule(false)} />));
    expect(container.textContent).toBe("Planner session: none yet");
  });

  it("asks before reset, leaves a cancelled reset alone, and refreshes after confirming", async () => {
    const fetch = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
      if (url === "/api/v1/schedule_admin/reset_planner_session") return Response.json({ status: "ok", result: { cleared: true } });
      if (url === "/api/v1/schedule_admin/get") return Response.json({ status: "ok", result: { schedule: schedule(false) } });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetch);
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    const onReset = vi.fn();
    await act(async () => root.render(<ResetPlannerSession schedule={schedule()} onReset={onReset} />));
    const button = container.querySelector("button")!;
    await act(async () => button.click());
    expect(fetch).not.toHaveBeenCalled();
    await act(async () => button.click());
    expect(confirm.mock.calls[0][0]).toContain("next run starts a new session");
    expect(confirm.mock.calls[0][0]).toContain("will not write it back");
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/schedule_admin/reset_planner_session");
    expect(fetch.mock.calls[0][1]?.method).toBe("POST");
    expect(fetch.mock.calls[0][1]?.body).toBe(JSON.stringify({ schedule_id: "sched/a" }));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1]).toEqual(["/api/v1/schedule_admin/get", expect.objectContaining({
      method: "POST", body: JSON.stringify({ schedule_id: "sched/a" }),
    })]);
    expect(onReset).toHaveBeenCalledWith(schedule(false));
  });

  it("allows reset without a stored session and shows a refused reset", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      status: "error", error: { code: "unavailable", message: "not available" },
    }, { status: 503 })));
    const onReset = vi.fn();
    await act(async () => root.render(<ResetPlannerSession schedule={schedule(false)} onReset={onReset} />));
    expect(container.querySelector("button")?.disabled).toBe(false);
    await act(async () => container.querySelector("button")!.click());
    expect(container.querySelector("[role=alert]")?.textContent).toContain("not available");
    expect(onReset).not.toHaveBeenCalled();
  });

  it("shows a pending reset's impact without refreshing or reporting it as completed", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const fetch = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "confirm-reset",
      expires_in_seconds: 300, impact: "Reset the stored Planner session; the next run starts fresh." }, { status: 202 }));
    vi.stubGlobal("fetch", fetch);
    const onReset = vi.fn();
    await act(async () => root.render(<ResetPlannerSession schedule={schedule()} onReset={onReset} />));
    await act(async () => container.querySelector("button")!.click());
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(onReset).not.toHaveBeenCalled();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("the next run starts fresh");
    expect(container.textContent).not.toContain("Planner session reset;");
  });

  it("defaults new schedules to user gates off, saves on, and saves a later change back to off", async () => {
    const writes: Record<string, unknown>[] = [];
    let stored = {
      ...schedule(false), manager_id: "manager", project_id: "project", project_name: "Example",
      template_key: null, template_inputs: {}, name: "Daily check", cron: "0 9 * * *", timezone: "UTC",
      description: "Every morning", requirement: "Read reports", work_dir: "", overlap: "skip", catch_up: "once",
      max_parallel_runs: 3, run_timeout_seconds: "default", run_timeout_effective_seconds: 60,
      run_budget_usd: null, profile_overrides: {}, task_limits: { worker: null, reviewer: null },
      gate_command: null, gate_timeout_seconds: null, daily_budget_usd: null, notify: "failures",
      notify_target: null, user_gates: "off", spent_today_usd: 0, enabled: false, last_run: null,
      alerts: { failures_since: null, stale_since: null },
    } as ScheduleInfo;
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      let result: unknown;
      if (url === "/api/v1/schedule_admin/project_list") result = { schedules: [], limits: {
        default_timezone: "UTC", name_max_chars: 80, requirement_max_chars: 10000,
        run_timeout_default_seconds: 60, run_timeout_max_seconds: 2592000, roles: ["planner"],
        gate_command_max_chars: 2000, gate_timeout_default_seconds: 30, gate_timeout_max_seconds: 300,
        max_parallel_runs_limit: 10, concurrency_max: 100, notify_targets: [],
      } };
      else if (url === "/api/v1/managers/list") result = { managers: [{ id: "manager", enabled: true }], max_enabled: 8 };
      else if (url === "/api/v1/registry/view") result = { profiles: [] };
      else if (url === "/api/v1/schedule_admin/create" || url === "/api/v1/schedule_admin/update") {
        const { fields } = JSON.parse(String(init?.body));
        writes.push(fields);
        stored = { ...stored, ...fields };
        result = { schedule: stored };
      } else if (url === "/api/v1/schedule_admin/runs") result = { runs: [], next_before: null,
        routine_skips: { skipped_gate: 0, skipped_overlap: 0 } };
      else if (url === "/api/v1/schedule_admin/preview") result = { valid: true, next: [], description: "Every morning" };
      else throw new Error(`Unexpected request: ${url}`);
      return Response.json({ status: "ok", result });
    }));
    const project = { project_id: "project", name: "Example", work_dir: "/srv/example" } as Project;
    await act(async () => root.render(<ProjectSchedulesSection project={project} linked="" onLink={() => {}} />));
    const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
    await act(async () => button("New schedule").click());
    const gates = () => container.querySelector<HTMLButtonElement>('[role="switch"][aria-label="User gates"]')!;
    expect(gates().getAttribute("aria-checked")).toBe("false");
    expect(container.textContent).not.toContain("wait counts against the run timeout");
    for (const [suffix, value] of [["name", "Daily check"], ["cron", "0 9 * * *"], ["requirement", "Read reports"]]) {
      const input = container.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[id$="-${suffix}"]`)!;
      await act(async () => {
        const prototype = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }
    await act(async () => gates().click());
    expect(gates().getAttribute("aria-checked")).toBe("true");
    expect(container.textContent).toContain("The gate's wait counts against the run timeout.");
    const noLimit = container.querySelector<HTMLInputElement>('input[type="radio"][value="none"]')!;
    await act(async () => noLimit.click());
    expect(container.textContent).not.toContain("wait counts against the run timeout");
    await act(async () => button("Create").click());
    expect(writes[0]).toMatchObject({ user_gates: "on", run_timeout_seconds: null });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-controls="schedule-sched/a-panel"]')!.click());
    expect(gates().getAttribute("aria-checked")).toBe("true");
    await act(async () => gates().click());
    await act(async () => button("Save").click());
    expect(writes[1]).toEqual({ user_gates: "off" });
  });
});
