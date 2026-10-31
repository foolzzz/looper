// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlannerSessionStatus, ProjectSchedulesSection, ResetPlannerSession, RunStatusBadge } from "@/components/project-schedules";
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

  it.each([["succeeded", "Succeeded", "text-success"], ["interrupted", "Interrupted", "text-destructive"]] as const)(
    "shows %s with both text and status color", async (status, label, color) => {
      await act(async () => root.render(<RunStatusBadge status={status} />));
      expect(container.textContent).toBe(label);
      expect(container.querySelector("span")?.className).toContain(color);
    },
  );

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
    expect(writes[0]).toEqual({ kind: "agent", name: "Daily check", cron: "0 9 * * *", requirement: "Read reports",
      timezone: "UTC", overlap: "skip", catch_up: "once", run_timeout_seconds: null, notify: "failures", user_gates: "on" });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-controls="schedule-sched/a-panel"]')!.click());
    expect(gates().getAttribute("aria-checked")).toBe("true");
    await act(async () => gates().click());
    await act(async () => button("Save").click());
    expect(writes[1]).toEqual({ user_gates: "off" });
  });
});

const storedSchedule = (kind: "agent" | "command" = "agent"): ScheduleInfo => ({
  ...schedule(false), kind, manager_id: "manager", project_id: "project", project_name: "Example",
  template_key: null, template_inputs: {}, name: "Daily check", cron: "0 9 * * *", timezone: "UTC",
  description: "Every morning", requirement: "Read reports", work_dir: "", overlap: "skip", catch_up: "once",
  max_parallel_runs: 3, run_timeout_seconds: kind === "command" ? null : "default",
  run_timeout_effective_seconds: null, run_budget_usd: null, profile_overrides: {},
  task_limits: { worker: null, reviewer: null }, gate_command: null, gate_timeout_seconds: null,
  daily_budget_usd: null, notify: "failures", notify_target: null, user_gates: "off",
  spent_today_usd: 0, enabled: false, last_run: null, command: kind === "command" ? "echo ready" : null,
  env: { ZONE: "west", APP: "reports" }, env_file: null, success_deadline: null,
  alert_policy: { failures_after: { value: 3, default: true }, stale_after_seconds: { value: 900, default: true },
    overlap_after: { value: kind === "command" ? 10 : 0, default: true } },
  alerts: { failures_since: null, stale_since: null, deadline_since: null, overlap_since: null },
} as ScheduleInfo);

describe("schedule form types", () => {
  let container: HTMLDivElement;
  let root: Root;
  let writes: Record<string, unknown>[];
  const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
  const input = (suffix: string) => container.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[id$="-${suffix}"]`)!;
  const fill = async (suffix: string, value: string) => {
    await act(async () => {
      const field = input(suffix);
      const prototype = field.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const render = async (existing?: ScheduleInfo) => {
    let stored = existing ?? storedSchedule();
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      let result: unknown;
      if (url === "/api/v1/schedule_admin/project_list") result = { schedules: existing ? [stored] : [], limits: {
        default_timezone: "UTC", name_max_chars: 80, requirement_max_chars: 10000,
        min_interval_seconds: 300, min_interval_gated_seconds: 60,
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
    await act(async () => root.render(<ProjectSchedulesSection
      project={{ project_id: "project", name: "Example", work_dir: "/srv/example" } as Project}
      linked="" onLink={() => {}} />));
    await act(async () => existing
      ? container.querySelector<HTMLButtonElement>('[aria-controls="schedule-sched/a-panel"]')!.click()
      : button("New schedule").click());
  };
  const command = async () => act(async () => container.querySelector<HTMLInputElement>('input[type="radio"][value="command"]')!.click());
  const required = async () => { await fill("name", "Daily check"); await fill("cron", "0 9 * * *"); };
  beforeEach(() => {
    writes = [];
    container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

  it("switches to command fields and submits only command settings", async () => {
    await render();
    expect(input("requirement")).toBeTruthy();
    await command();
    expect(input("requirement")).toBeNull();
    expect(input("command")).toBeTruthy();
    expect(container.textContent).not.toContain("Use the default");
    expect(container.textContent).not.toContain("Spend cap per run");
    expect(container.textContent).not.toContain("User gates");
    await required(); await fill("command", "echo ready");
    await act(async () => button("Create").click());
    expect(writes).toEqual([{
      kind: "command", name: "Daily check", cron: "0 9 * * *", timezone: "UTC", command: "echo ready",
      env: {}, env_file: null, overlap: "skip", catch_up: "once", run_timeout_seconds: null,
      notify: "failures", success_deadline: null, alert_failures_after: null,
      alert_stale_after_seconds: null, alert_overlap_after: null,
    }]);
  });

  it("edits commands with an immutable type and sorted environment, without resending kind or defaults", async () => {
    await render(storedSchedule("command"));
    expect(container.querySelector('input[value="command"]')).toBeNull();
    expect(container.textContent).toContain("Type: Command");
    expect(input("env").value).toBe("APP=reports\nZONE=west");
    expect(button("Save").disabled).toBe(true);
    await fill("command", "echo updated");
    await act(async () => button("Save").click());
    expect(writes).toEqual([{ command: "echo updated" }]);
  });

  it("blocks malformed environment lines, keeps values after the first equals, and warns on duplicates", async () => {
    await render(); await command(); await required(); await fill("command", "echo ready");
    await fill("env", "# comment\n\nBROKEN");
    expect(container.textContent).toContain("Line 3 is not KEY=VALUE");
    expect(button("Create").disabled).toBe(true);
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(writes).toEqual([]);
    await fill("env", "APP=old\nAPP=new=value\nEMPTY=");
    expect(container.textContent).toContain("last value");
    await act(async () => button("Create").click());
    expect(writes[0].env).toEqual({ APP: "new=value", EMPTY: "" });
  });

  it("clears an env file and preserves a command timeout limit", async () => {
    const stored = storedSchedule("command");
    stored.env_file = "/srv/example/report.env";
    stored.run_timeout_seconds = 120;
    await render(stored);
    expect(input("env-file").value).toBe("/srv/example/report.env");
    expect(input("timeout").value).toBe("2");
    expect(button("Save").disabled).toBe(true);
    await fill("env-file", "");
    await act(async () => button("Save").click());
    expect(writes).toEqual([{ env_file: null }]);
  });

  it("blocks an empty or oversized command and validates alert boundaries", async () => {
    await render(); await command(); await required();
    expect(button("Create").disabled).toBe(true);
    await fill("command", "x".repeat(2001));
    expect(button("Create").disabled).toBe(true);
    await fill("command", "echo ready");
    await fill("stale-after", "4");
    expect(button("Create").disabled).toBe(true);
    expect(container.textContent).toContain("from 5 to 43200 minutes");
    await fill("stale-after", "5");
    await fill("deadline", "25:00");
    expect(button("Create").disabled).toBe(true);
    await fill("deadline", "23:59");
    expect(button("Create").disabled).toBe(false);
  });

  it("preserves the agent submission body apart from its explicit type", async () => {
    await render(); await required(); await fill("requirement", "Read reports");
    await act(async () => button("Create").click());
    const { kind, ...body } = writes[0];
    expect(kind).toBe("agent");
    expect(body).toEqual({ name: "Daily check", cron: "0 9 * * *", requirement: "Read reports", timezone: "UTC",
      overlap: "skip", catch_up: "once", run_timeout_seconds: "default", notify: "failures", user_gates: "off" });
  });

  it.each(["agent", "command"] as const)("roundtrips custom alerts and resets %s thresholds to defaults", async (kind) => {
    const stored = storedSchedule(kind);
    stored.success_deadline = "08:00";
    stored.alert_policy.failures_after = { value: 5, default: false };
    stored.alert_policy.stale_after_seconds = { value: 7200, default: false };
    stored.alert_policy.overlap_after = { value: 4, default: false };
    await render(stored);
    expect(button("Save").disabled).toBe(true);
    expect(input("stale-after").value).toBe("120");
    await fill("deadline", "09:30"); await fill("failures-after", "");
    await fill("stale-after", "5.5"); await fill("overlap-after", "0");
    await act(async () => button("Save").click());
    expect(writes).toEqual([{ success_deadline: "09:30", alert_failures_after: null,
      alert_stale_after_seconds: 330, alert_overlap_after: 0 }]);
  });
});
