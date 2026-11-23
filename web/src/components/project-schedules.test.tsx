// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ScheduleDetail } from "@/components/schedule-detail";
import { PlannerSessionStatus, ProjectSchedulesSection, ResetPlannerSession, RunStatusBadge, RunHistory, LastRun, SubscriptionStatus, listNextRun, usageWindowWait } from "@/components/project-schedules";
import { SchedulesPage } from "@/pages/schedules";
import type { Project, ScheduleInfo, ScheduleRunInfo } from "@/lib/api";

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
      gate_command: null, gate_timeout_seconds: null, daily_budget_usd: null, notify: "failures", notify_format: "report",
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
      else if (url === "/api/v1/registry/view") result = { providers: [], profiles: [] };
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
      timezone: "UTC", overlap: "skip", catch_up: "once", run_timeout_seconds: null, notify: "failures", notify_format: "report", user_gates: "on" });
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
  daily_budget_usd: null, notify: "failures", notify_format: "report", notify_target: null, user_gates: "off",
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
      else if (url === "/api/v1/registry/view") result = { providers: [], profiles: [] };
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

  it("saves the selected audience format and restricts commands to reports", async () => {
    await render(storedSchedule());
    const format = container.querySelector<HTMLSelectElement>('[id$="-notify-format"]')!;
    expect(format).not.toBeNull();
    expect(format.value).toBe("report");
    await act(async () => {
      format.value = "deliverable";
      format.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => button("Save").click());
    expect(writes).toEqual([{ notify_format: "deliverable" }]);
    await act(async () => root.unmount());
    root = createRoot(container);
    await render(storedSchedule("command"));
    const commandFormat = container.querySelector<HTMLSelectElement>('[id$="-notify-format"]')!;
    expect([...commandFormat.options].map((option) => option.value)).toEqual(["report"]);
    expect(commandFormat.disabled).toBe(true);
  });

  it("switches to command fields and submits only command settings", async () => {
    await render();
    expect(input("requirement")).toBeTruthy();
    const format = container.querySelector<HTMLSelectElement>('[id$="-notify-format"]')!;
    await act(async () => {
      format.value = "deliverable";
      format.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await command();
    expect(format.value).toBe("report");
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
      notify: "failures", notify_format: "report", success_deadline: null, alert_failures_after: null,
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
      overlap: "skip", catch_up: "once", run_timeout_seconds: "default", notify: "failures", notify_format: "report", user_gates: "off" });
  });

  it.each(["agent", "command"] as const)("shows saved %s alert settings and kind-specific notification hints", async (kind) => {
    const stored = storedSchedule(kind);
    stored.success_deadline = "08:00";
    stored.alert_policy.failures_after = { value: 2, default: false };
    stored.alert_policy.stale_after_seconds = { value: 7200, default: false };
    stored.alert_policy.overlap_after = { value: 4, default: false };
    await render(stored);
    expect(container.querySelector("details")?.open).toBe(true);
    expect(container.textContent).toContain("2 failures in a row");
    expect(container.textContent).toContain(kind === "command" ? "Every run that ran a command" : "Every run that ran a task");
    expect(container.textContent?.includes("Ownership cannot be changed")).toBe(kind === "agent");
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, init) => String(url).endsWith("schedule_admin/get")
      ? Response.json({ status: "ok", result: { schedule: stored } }) : base(url, init));
    await act(async () => root.render(<ScheduleDetail scheduleId={stored.schedule_id} onDeleted={() => {}} />));
    expect(container.textContent).toContain("2 failures in a row");
    const settings = container.querySelector('[aria-labelledby="schedule-settings"]')!;
    expect(settings.textContent).toContain("08:00");
    expect(settings.textContent).toContain("2 h");
    expect(settings.textContent).toContain("4 skipped firings");
  });

  it.each([["env_file", "env-file"], ["command", "command"]])("shows a server refusal for %s beside its field and at the top", async (serverField, suffix) => {
    await render(); await command(); await required(); await fill("command", "echo ready");
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, init) => String(url).endsWith("/create")
      ? Response.json({ status: "error", error: { code: "invalid", message: `${serverField} was refused` } }, { status: 400 })
      : base(url, init));
    await act(async () => button("Create").click());
    expect(input(suffix).parentElement?.textContent).toContain(`${serverField} was refused`);
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(2);
    expect(input(suffix).getAttribute("aria-invalid")).toBe("true");
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


describe("command run history", () => {
  let container: HTMLDivElement;
  let root: Root;
  const run = (changes: Partial<ScheduleRunInfo> = {}): ScheduleRunInfo => ({
    run_id: "srun-failed", schedule_id: "sched/a", status: "failed", trigger: "catch_up", missed: 3,
    scheduled_for: "2026-10-30T09:00:00Z", fired_at: "2026-10-30T09:00:00Z",
    started_at: "2026-10-30T09:01:00Z", finished_at: "2026-10-30T09:01:07Z",
    exit_code: 9, exit_signal: null, duration_seconds: 7, firings: 1,
    log: { file: "run.log", bytes: 12, truncated: false }, log_tail: "stored tail",
    task_id: null, task_title: null, task_status: null, summary: null, error: null,
    cost_usd: null, gate_output: null, carried_from: null, ...changes,
  } as ScheduleRunInfo);
  const stats = { runs: 48, succeeded: 47, failed: 1, success_rate: 47 / 48, avg_seconds: 7, max_seconds: 20 };
  const requests: { path: string; body: Record<string, unknown> }[] = [];
  const respond = (runs = [run()], next: string | null = null) => ({ runs, next_before: next,
    routine_skips: { skipped_gate: 0, skipped_overlap: 2 }, stats: { day: stats, week: stats } });
  const stub = (handler?: (path: string, body: Record<string, unknown>) => unknown) => {
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url).split("/api/v1/")[1];
      const body = JSON.parse(String(init?.body ?? "{}"));
      requests.push({ path, body });
      const result = handler?.(path, body) ?? (path === "schedule_admin/run_log"
        ? { size: 12, offset: 0, next_offset: null, data_base64: btoa("fresh tail"), complete: true, truncated: false }
        : respond());
      return result instanceof Response ? result : Response.json({ status: "ok", result });
    }));
  };
  const button = (name: string) => [...container.querySelectorAll("button")].find((item) => item.textContent?.includes(name))!;
  beforeEach(() => {
    container = document.createElement("div"); document.body.append(container); root = createRoot(container);
    requests.length = 0;
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("shows command exit, actual duration, catch-up and both time-window statistics", async () => {
    stub();
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} />));
    expect(container.textContent).toContain("exit 9");
    expect(container.textContent).toContain("7 s");
    expect(container.textContent).not.toContain("1 min 07 s");
    expect(container.textContent).toContain("Catch-up (3 missed)");
    expect(container.textContent).toContain("Last 24 h: 47/48 succeeded · avg 7 s · max 20 s");
    expect(container.textContent).toContain("Last 7 days:");
    expect(container.textContent).toContain("Started");
  });

  it("passes the status filter and shows overlap skips even when routine skips were hidden", async () => {
    stub();
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} />));
    const select = container.querySelector<HTMLSelectElement>('select[aria-label="Run status"]')!;
    await act(async () => { select.value = "skipped_overlap"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(requests.at(-1)?.body).toMatchObject({ status: "skipped_overlap", skips: "show" });
    expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.disabled).toBe(true);
  });

  it("shows the stored tail immediately, loads the last 200 lines, and refreshes a growing log only on demand", async () => {
    vi.useFakeTimers();
    let finish: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => String(url).endsWith("/runs")
      ? Response.json({ status: "ok", result: respond([run({ status: "running", finished_at: null })]) })
      : new Promise<Response>((resolve) => { finish = resolve; })));
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} />));
    await act(async () => button("Running").click());
    expect(container.textContent).toContain("stored tail");
    expect(container.textContent).toContain("Still running: the log grows");
    await act(async () => finish!(Response.json({ status: "ok", result: {
      size: 12, offset: 0, next_offset: null, data_base64: btoa("fresh tail"), complete: false, truncated: true,
    } })));
    expect(container.querySelector('[aria-label="Run log"]')?.textContent).toBe("fresh tail");
    expect(container.textContent).toContain("The log was truncated at 50 MiB; the middle is left out.");
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    const logCalls = () => vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/run_log"));
    expect(logCalls()).toHaveLength(1);
    expect(JSON.parse(String(logCalls()[0][1]?.body))).toEqual({ run_id: "srun-failed", tail_lines: 200, limit_bytes: 4 * 1024 * 1024 });
    await act(async () => button("Refresh log").click());
    expect(logCalls()).toHaveLength(2);
  });

  it("finds and expands a linked run on an older page including hidden skips", async () => {
    stub((path, body) => path === "schedule_admin/runs"
      ? body.before ? respond([run({ run_id: "srun-target", exit_code: null, exit_signal: 15 })]) : respond([run()], "srun-failed")
      : undefined);
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-target" />));
    expect(requests.filter((r) => r.path.endsWith("/runs")).map((r) => r.body.skips)).toEqual(["show", "show"]);
    expect(container.querySelector('[data-run-id="srun-target"] button')?.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector('[data-run-id="srun-target"]')?.textContent).toContain("signal 15");
    expect(container.textContent).toContain("fresh tail");
  });

  it("shows a disabled schedule waiting in its own zone, on the project, the detail and the list", async () => {
    const until = "2026-01-15T21:00:00+00:00";
    const stored = storedSchedule();
    stored.timezone = "America/Los_Angeles";
    stored.usage_hold = { until, reason: "rejected", reset_known: true, window: "five_hour" };
    const waiting = usageWindowWait(until, stored.timezone);
    expect(waiting).toContain("Waiting for a usage window until ");
    expect(waiting).not.toContain(until);
    expect(listNextRun(stored)).toBe(`Disabled · ${waiting}`);
    stub((path) => {
      if (path === "schedule_admin/project_list") return { schedules: [stored], limits: {
        default_timezone: "UTC", roles: [], notify_targets: [], name_max_chars: 80, requirement_max_chars: 10000,
      } };
      if (path === "schedule_admin/get") return { schedule: stored };
      if (path === "schedule_admin/runs") return { runs: [], next_before: null, routine_skips: {} };
      if (path === "schedule_admin/list") return { schedules: [{ ...stored, project_id: "project", project_name: "Example" }] };
      if (path === "project_admin/list") return { projects: [{ project_id: "project", name: "Example" }], projects_root: "/tmp" };
      if (path === "managers/list") return { managers: [] };
      if (path === "registry/view") return { providers: [], profiles: [] };
    });
    await act(async () => root.render(<ProjectSchedulesSection project={{ project_id: "project" } as Project}
      linked="" onLink={() => {}} />));
    expect(container.textContent).toContain(`Disabled · ${waiting}`);
    expect(container.textContent).not.toContain(until);
    await act(async () => root.render(<ScheduleDetail scheduleId={stored.schedule_id} onDeleted={() => {}} />));
    expect(container.textContent).toContain("Disabled: it does not run on its rule");
    expect(container.textContent).toContain(waiting);
    expect(container.textContent).not.toContain(until);
    await act(async () => root.render(<SchedulesPage sub="" query="" onNavigate={() => {}} />));
    expect(container.textContent).toContain(`Disabled · ${waiting}`);
    expect(container.textContent).not.toContain(until);
  });

  it("opens the schedule and target run from its nested project link", async () => {
    stub((path) => {
      if (path === "schedule_admin/project_list") return { schedules: [storedSchedule("command")], limits: {
        default_timezone: "UTC", roles: [], notify_targets: [], name_max_chars: 80, requirement_max_chars: 10000,
      } };
      if (path === "managers/list") return { managers: [] };
      if (path === "registry/view") return { providers: [], profiles: [] };
    });
    await act(async () => root.render(<ProjectSchedulesSection project={{ project_id: "project" } as Project}
      linked="sched%2Fa/runs/srun-failed" onLink={() => {}} />));
    expect(container.querySelector('[aria-controls="schedule-sched/a-panel"]')?.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector('[data-run-id="srun-failed"] button')?.getAttribute("aria-expanded")).toBe("true");
  });

  it("reopens the schedule for a different linked run after collapse", async () => {
    stub((path) => path === "schedule_admin/project_list" ? { schedules: [storedSchedule("command")], limits: {
      default_timezone: "UTC", roles: [], notify_targets: [], name_max_chars: 80, requirement_max_chars: 10000,
    } } : path === "managers/list" ? { managers: [] } : path === "registry/view" ? { providers: [], profiles: [] }
      : path === "schedule_admin/runs" ? respond([run(), run({ run_id: "run-b" })]) : undefined);
    const show = (runId: string) => root.render(<ProjectSchedulesSection project={{ project_id: "project" } as Project}
      linked={`sched%2Fa/runs/${runId}`} onLink={() => {}} />);
    await act(async () => show("srun-failed"));
    const toggle = container.querySelector<HTMLButtonElement>('[aria-controls="schedule-sched/a-panel"]')!;
    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    await act(async () => show("run-b"));
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector('[data-run-id="run-b"] button')?.getAttribute("aria-expanded")).toBe("true");
    expect(Element.prototype.scrollIntoView).toHaveBeenLastCalledWith({ block: "center" });
  });

  it.each([true, false])("walks byte windows before decoding a 200-line tail (newline: %s)", async (newline) => {
    const text = "discard\n" + Array.from({ length: 200 }, (_, i) => `${i}:${"x".repeat(30000)}`).join("\n") + (newline ? "\n" : "");
    const data = new TextEncoder().encode(text);
    const edge = data.length - 4 * 1024 * 1024;
    data.set(new TextEncoder().encode("界"), edge - 1);
    const encode = (bytes: Uint8Array) => { let binary = ""; for (const byte of bytes) binary += String.fromCharCode(byte); return btoa(binary); };
    stub((path, body) => path === "schedule_admin/run_log" ? {
      size: data.length, offset: body.offset ?? edge, next_offset: null, complete: true, truncated: false,
      data_base64: encode(body.offset == null ? data.slice(edge) : data.slice(Number(body.offset), edge)),
    } : undefined);
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-failed" />));
    expect(container.querySelector('[aria-label="Run log"]')?.textContent === new TextDecoder().decode(data.slice("discard\n".length))).toBe(true);
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(2);
    expect(container.textContent).not.toContain("download the full log for the rest.");
  });

  it.each([true, false])("reports whole lines when the tail walk reaches its window limit (newline: %s)", async (newline) => {
    const window = 4 * 1024 * 1024;
    const ending = `\nsecond\nthird${newline ? "\n" : ""}`;
    stub((path, body) => path === "schedule_admin/run_log" ? {
      size: 5 * window, offset: body.offset ?? 4 * window, next_offset: null, complete: true, truncated: false,
      data_base64: btoa(body.offset == null ? "x".repeat(window - ending.length) + ending : "x".repeat(window)),
    } : undefined);
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-failed" />));
    expect(container.querySelector('[aria-label="Run log"]')?.textContent).toBe(`second\nthird${newline ? "\n" : ""}`);
    expect(container.textContent).toContain("Showing the last 2 lines; download the full log for the rest.");
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(4);
  });

  it("shows exit zero on succeeded command runs", async () => {
    stub(() => respond([run({ status: "succeeded", exit_code: 0 })]));
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} />));
    expect(container.textContent).toContain("exit 0");
    expect(container.querySelector('[data-run-id] button svg')).not.toBeNull();
  });

  it("refetches the open tail once when the run finishes", async () => {
    let running = true;
    stub((path) => path === "schedule_admin/runs" ? respond([run({ status: running ? "running" : "succeeded", exit_code: running ? null : 0 })])
      : { size: 12, offset: 0, next_offset: null, data_base64: btoa(running ? "before" : "after"), complete: !running, truncated: false });
    const show = (version: number) => root.render(<RunHistory schedule={storedSchedule("command")} open version={version} runId="srun-failed" />);
    await act(async () => show(0));
    expect(container.textContent).toContain("Still running: the log grows");
    running = false;
    await act(async () => show(1));
    await act(async () => show(2));
    expect(container.textContent).not.toContain("Still running: the log grows");
    expect(container.querySelector('[aria-label="Run log"]')?.textContent).toBe("after");
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(2);
  });

  it("fetches once when a queued run acquires a log as it finishes", async () => {
    let queued = true;
    stub((path) => path === "schedule_admin/runs" ? respond([run({ status: queued ? "queued" : "succeeded", log: queued ? null : run().log })]) : undefined);
    const show = (version: number) => root.render(<RunHistory schedule={storedSchedule("command")} open version={version} runId="srun-failed" />);
    await act(async () => show(0));
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(0);
    queued = false;
    await act(async () => show(1));
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(1);
    expect(container.querySelector('[aria-label="Run log"]')?.textContent).toBe("fresh tail");
  });

  it("keeps the finished log when a running request returns late", async () => {
    let running = true;
    let finish!: (response: Response) => void;
    stub((path) => path === "schedule_admin/runs" ? respond([run({ status: running ? "running" : "succeeded" })]) : undefined);
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, init) => String(url).endsWith("/run_log") && running
      ? new Promise<Response>((resolve) => { finish = resolve; }) : base(url, init));
    const show = (version: number) => root.render(<RunHistory schedule={storedSchedule("command")} open version={version} runId="srun-failed" />);
    await act(async () => show(0));
    running = false;
    await act(async () => show(1));
    await act(async () => finish(Response.json({ status: "ok", result: {
      size: 5, offset: 0, data_base64: btoa("stale"), complete: false, truncated: false,
    } })));
    expect(container.querySelector('[aria-label="Run log"]')?.textContent).toBe("fresh tail");
    expect(container.textContent).not.toContain("Still running: the log grows");
  });

  it("does not request a log for an overlap skip and shows no duration", async () => {
    const summary = "the previous command run (srun-previous) is still in progress";
    stub(() => respond([run({ status: "skipped_overlap", log: null, duration_seconds: 0, exit_code: null, summary })]));
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-failed" />));
    expect(container.textContent).toContain(`${summary}This run did not start the command, so it has no log.`);
    expect(container.textContent).toContain("This run did not start the command, so it has no log.");
    expect(container.querySelector('[aria-label="Run log"]')).toBeNull();
    expect(button("Download log")).toBeUndefined();
    expect(button("Refresh log")).toBeUndefined();
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(0);
    expect(container.querySelector('[data-run-id] button')?.textContent).toContain("Duration—");
  });

  it("shows a failed launch's reason without log controls or duration", async () => {
    const error = "env_file line 2 must be KEY=VALUE";
    stub(() => respond([run({ started_at: null, log: { file: "run.log", bytes: 0, truncated: false },
      duration_seconds: null, exit_code: null, error, summary: "Launch failed" })]));
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-failed" />));
    expect(container.textContent).toContain(`${error}This run did not start the command, so it has no log.`);
    expect(container.textContent).not.toContain("Launch failed");
    expect(container.querySelector('[aria-label="Run log"]')).toBeNull();
    expect(button("Download log")).toBeUndefined();
    expect(button("Refresh log")).toBeUndefined();
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(0);
    expect(container.querySelector('[data-run-id] button')?.textContent).toContain("Duration—");
  });

  it("keeps a complete short log without a partial-preview note", async () => {
    stub();
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-failed" />));
    expect(container.querySelector('[aria-label="Run log"]')?.textContent).toBe("fresh tail");
    expect(container.textContent).not.toContain("download the full log for the rest.");
  });

  it.each([true, false])("keeps log controls for an empty log when active or started (active: %s)", async (active) => {
    stub((path) => path === "schedule_admin/runs" ? respond([run({ status: active ? "running" : "succeeded",
      started_at: active ? null : "2026-10-30T09:01:00Z", finished_at: active ? null : "2026-10-30T09:01:07Z",
      log: { file: "run.log", bytes: 0, truncated: false } })]) : undefined);
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-failed" />));
    expect(button("Download log")).toBeDefined();
    expect(button("Refresh log")).toBeDefined();
    expect(requests.filter((r) => r.path.endsWith("/run_log"))).toHaveLength(1);
  });

  it.each([[2, 2, "destructive"], [undefined, 2, "warning"], [undefined, 3, "destructive"]] as const)(
    "colours failures at threshold %s with count %s", async (threshold, failures, colour) => {
      const schedule = storedSchedule("command");
      schedule.consecutive_failures = failures;
      if (threshold != null) schedule.alert_policy.failures_after = { value: threshold, default: false };
      else delete (schedule as Partial<ScheduleInfo>).alert_policy;
      await act(async () => root.render(<LastRun schedule={schedule} />));
      const badge = [...container.querySelectorAll("span")].find((el) => el.textContent === `${failures} failures in a row`)!;
      expect(badge.className).toContain(`text-${colour}`);
    });

  it("reports a retained-history miss and an empty statistics window", async () => {
    stub(() => ({ ...respond([]), stats: { day: { ...stats, runs: 0 }, week: { ...stats, runs: 0 } } }));
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-gone" />));
    expect(container.textContent).toContain("This run is no longer kept");
    expect(container.textContent).toContain("No runs in the last 24 h");
  });

  it.each([true, false])("searches past 200 runs until the target is found or history is exhausted (retained: %s)", async (retained) => {
    const rows = Array.from({ length: 261 }, (_, index) => run({ run_id: `srun-${index}`, status: "skipped_overlap" }));
    if (retained) rows[220] = run({ run_id: "srun-target", status: "running", finished_at: null });
    stub((path, body) => {
      if (path !== "schedule_admin/runs") return;
      const start = body.before ? rows.findIndex((row) => row.run_id === body.before) + 1 : 0;
      const end = start + Number(body.limit);
      return respond(rows.slice(start, end), end < rows.length ? rows[end - 1].run_id : null);
    });
    let resume!: () => void;
    const pending = new Promise<void>((resolve) => { resume = resolve; });
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, init) => {
      if (String(url).endsWith("/runs") && JSON.parse(String(init?.body)).before === "srun-199") await pending;
      return base(url, init);
    });
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-target" />));
    try {
      expect(container.textContent).not.toContain("This run is no longer kept");
    } finally {
      await act(async () => resume());
    }
    const calls = requests.filter((request) => request.path === "schedule_admin/runs");
    expect(calls).toHaveLength(retained ? 12 : 14);
    expect(calls.every(({ body }) => body.skips === "show" && Number(body.limit) > 0 && Number(body.limit) <= 200)).toBe(true);
    if (retained) {
      expect(container.querySelector('[data-run-id="srun-target"] button')?.getAttribute("aria-expanded")).toBe("true");
      expect(container.textContent).toContain("fresh tail");
      expect(container.textContent).not.toContain("This run is no longer kept");
      expect(button("Show older runs")).toBeDefined();
    } else {
      expect(container.querySelectorAll("li[data-run-id]")).toHaveLength(rows.length);
      expect(container.textContent).toContain("This run is no longer kept");
      expect(button("Show older runs")).toBeUndefined();
    }
  });

  it("shows the API reason when a log has been removed", async () => {
    stub((path) => path === "schedule_admin/run_log" ? Response.json({ status: "error",
      error: { code: "not_found", message: "the log was removed by the retention policy" } }, { status: 404 }) : undefined);
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} />));
    await act(async () => button("Failed").click());
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("the log was removed by the retention policy");
    expect(container.querySelector('[aria-label="Run log"]')?.textContent).toBe("stored tail");
  });

  it("shows byte progress while a running log download is pending, then downloads its snapshot", async () => {
    let finish: (value: Response) => void;
    stub((path, body) => path === "schedule_admin/runs"
      ? respond([run({ status: "running", finished_at: null })])
      : body.offset === 0
        ? { size: 6, offset: 0, next_offset: 3, data_base64: btoa("abc"), complete: false, truncated: false }
        : undefined);
    const fetcher = vi.mocked(fetch);
    const base = fetcher.getMockImplementation()!;
    fetcher.mockImplementation(async (url, init) => {
      if (String(url).endsWith("/run_log") && JSON.parse(String(init?.body)).offset === 3)
        return new Promise<Response>((resolve) => { finish = resolve; });
      return base(url, init);
    });
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const create = vi.fn((_blob: Blob | MediaSource) => "blob:run-log");
    vi.spyOn(URL, "createObjectURL").mockImplementation(create);
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} />));
    await act(async () => button("Running").click());
    await act(async () => button("Download log").click());
    expect(button("Downloading 3 / 6 bytes").disabled).toBe(true);
    await act(async () => finish!(Response.json({ status: "ok", result: {
      size: 6, offset: 3, next_offset: null, data_base64: btoa("def"), complete: false, truncated: false,
    } })));
    expect(button("Download log").disabled).toBe(false);
    expect(clicked).toHaveBeenCalledOnce();
    expect(await (create.mock.calls[0][0] as Blob).text()).toBe("abcdef");
  });

  it("keeps agent task, cost, gate output and summary rendering without command controls", async () => {
    stub(() => respond([run({ status: "completed", task_id: "task-a", task_title: "Review report", task_status: "completed",
      summary: "Report ready", gate_output: "New input", cost_usd: 0.5, duration_seconds: 67 })]));
    await act(async () => root.render(<RunHistory schedule={storedSchedule()} open version={0} />));
    expect(container.querySelector('a[href="#tasks/task-a"]')?.textContent).toContain("Review report");
    expect(container.textContent).toContain("Report ready"); expect(container.textContent).toContain("New input");
    expect(container.textContent).toContain("1 min 07 s"); expect(container.textContent).toContain("$0.50");
    expect(container.querySelector("select")).toBeNull(); expect(container.querySelector('[data-run-id]')).toBeNull();
    expect(container.textContent).not.toContain("Last 24 h");
  });

  it("shows deadline and overlap warnings for agent schedules too", async () => {
    const schedule = storedSchedule();
    schedule.alerts.deadline_since = "2026-10-30T08:00:00Z";
    schedule.alerts.overlap_since = "2026-10-30T07:00:00Z";
    await act(async () => root.render(<LastRun schedule={schedule} />));
    expect(container.textContent).toContain("Missed its success deadline since");
    expect(container.textContent).toContain("Skipping: the previous run is still going since");
  });
});

describe("subscription schedules", () => {
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

  const subscription = (confirmed: string | null): ScheduleInfo => ({
    ...schedule(false), kind: "subscription", enabled: true, notify_target: { channel: "lark", chat: "ou_synthetic" },
    description: "Every hour", next_fire_at: null, last_run: null, consecutive_failures: 0, requirement: "Pick the funny ones",
    alerts: { failures_since: null, stale_since: null, deadline_since: null, overlap_since: null },
    subscription: {
      source_id: "stream", chat_kinds: ["group"], include_chats: [], exclude_chats: [], sender_kinds: ["user"],
      exclude_mentions_me: true, include_history: false, max_age_minutes: 180, max_events: 500, text_max_chars: 200,
      quiet_word: "SKIP", output_header: "", cursor_seq: 41, cursor_at: null, target_confirmed_at: confirmed,
      config_version: "v-shown", confirmation: "确认推送目标：\n- 推送目标：Lark · ou_synthetic（私聊）\n- 本机有企业管理的 hooks，会看到处理的消息",
    },
  } as ScheduleInfo);

  it("says the output stays on the dashboard until the target is confirmed, then shows the facts and confirms", async () => {
    const fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (url === "/api/v1/schedule_admin/get") return Response.json({ status: "ok", result: { schedule: subscription(null) } });
      if (url === "/api/v1/schedule_admin/confirm_target")
        return Response.json({ status: "ok", result: { schedule: subscription("2026-11-19T10:00:00Z") } });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetch);
    const onChange = vi.fn();
    await act(async () => root.render(<SubscriptionStatus schedule={subscription(null)} onChange={onChange} />));
    expect(container.textContent).toContain("Push target not confirmed: output stays on this page.");
    expect(container.textContent).toContain("Cursor 41");
    const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
    await act(async () => button("Confirm push target…").click());
    expect(container.querySelector("pre")?.textContent).toContain("本机有企业管理的 hooks，会看到处理的消息");
    expect(fetch).not.toHaveBeenCalledWith("/api/v1/schedule_admin/confirm_target", expect.anything());
    await act(async () => button("Confirm").click());
    // It names the version the panel showed.
    expect(fetch).toHaveBeenLastCalledWith("/api/v1/schedule_admin/confirm_target", expect.objectContaining({
      body: JSON.stringify({ schedule_id: "sched/a", config_version: "v-shown" }) }));
    expect(onChange).toHaveBeenCalledWith(subscription("2026-11-19T10:00:00Z"));
  });

  it("refuses to confirm a target that changed after the panel showed it, and shows what it is now", async () => {
    let reads = 0;
    const fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (url === "/api/v1/schedule_admin/get") {
        reads += 1;
        return Response.json({ status: "ok", result: { schedule: { ...subscription(null),
          subscription: { ...subscription(null).subscription!, config_version: reads === 1 ? "v-shown" : "v-now" } } } });
      }
      if (url === "/api/v1/schedule_admin/confirm_target") return Response.json({ status: "error", error: {
        code: "conflict", message: "the subscription changed since it was shown" } }, { status: 409 });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetch);
    const onChange = vi.fn();
    await act(async () => root.render(<SubscriptionStatus schedule={subscription(null)} onChange={onChange} />));
    const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
    await act(async () => button("Confirm push target…").click());
    await act(async () => button("Confirm").click());
    expect(fetch).toHaveBeenCalledWith("/api/v1/schedule_admin/confirm_target", expect.objectContaining({
      body: JSON.stringify({ schedule_id: "sched/a", config_version: "v-shown" }) }));
    expect(onChange).toHaveBeenCalledTimes(1);  // the schedule as it is now replaces what the panel showed
    expect(onChange.mock.calls[0][0].subscription.config_version).toBe("v-now");
    expect(container.querySelector("[role=group] pre")).toBeNull();
  });

  it("says when a subscription selects by rules, which the form does not edit", async () => {
    const withRules = { ...subscription("2026-11-19T10:00:00Z") };
    withRules.subscription = { ...withRules.subscription!, rules: [{ name: "a", label: "单聊", never_quiet: true },
      { name: "b", label: "", never_quiet: false }] };
    await act(async () => root.render(<SubscriptionStatus schedule={withRules} onChange={vi.fn()} />));
    expect(container.textContent).toContain("Selects by 2 rules (set through the API)");
    await act(async () => root.render(<SubscriptionStatus schedule={subscription("2026-11-19T10:00:00Z")} onChange={vi.fn()} />));
    expect(container.textContent).not.toContain("Selects by");
  });

  it("shows a confirmed target without the button, and resets the cursor to a time with its offset", async () => {
    const fetch = vi.fn(async () => Response.json({ status: "ok", result: {
      schedule: subscription("2026-11-19T10:00:00Z"), cursor: { cursor_seq: 7, committed_seq: 20, events_after: 13 } } }));
    vi.stubGlobal("fetch", fetch);
    const onChange = vi.fn();
    await act(async () => root.render(<SubscriptionStatus schedule={subscription("2026-11-19T10:00:00Z")} onChange={onChange} />));
    expect(container.textContent).toContain("Pushes to lark · ou_synthetic, confirmed");
    expect(container.textContent).not.toContain("Confirm push target");
    const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
    await act(async () => button("Reset cursor…").click());
    const input = container.querySelector<HTMLInputElement>('input[type="datetime-local"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "2026-11-19T08:00");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Reset").click());
    const body = JSON.parse(String((fetch.mock.calls[0] as unknown[])[1] && ((fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body)));
    expect(body.schedule_id).toBe("sched/a");
    expect(new Date(body.after).getTime()).toBe(new Date("2026-11-19T08:00").getTime());
    expect(container.textContent).toContain("Cursor at 7: the next run reads the 13 events stored since.");
  });

  it("shows what a run read and made, with its output", async () => {
    const run = {
      run_id: "run-1", status: "completed", trigger: "scheduled", firings: 1, scheduled_for: "2026-11-19T10:05:00Z",
      subscription: { from_seq: 10, to_seq: 31, events_in_window: 21, events_selected: 18, events_too_old: 2, events_over_cap: 1,
        record_id: null, outcome: "dashboard_only", output_text: "- gossip", notification_id: null },
    } as unknown as ScheduleRunInfo;
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
      if (url === "/api/v1/schedule_admin/runs") return Response.json({ status: "ok", result: { runs: [run], next_before: null,
        routine_skips: { skipped_gate: 0, skipped_overlap: 0 } } });
      throw new Error(`Unexpected request: ${url}`);
    }));
    await act(async () => root.render(<RunHistory schedule={subscription(null)} open version={0} />));
    expect(container.textContent).toContain("Kept here (target not confirmed)");
    expect(container.textContent).toContain("18 of 21 messages read (2 too old, 1 over the cap)");
    expect(container.textContent).toContain("events 11–31");
    expect(container.querySelector("details pre")?.textContent).toBe("- gossip");
  });

  it("says why a run skipped its window", async () => {
    const run = {
      run_id: "run-2", status: "completed", trigger: "scheduled", firings: 1, scheduled_for: "2026-11-19T10:05:00Z",
      subscription: { from_seq: 10, to_seq: 31, events_in_window: 21, events_selected: 21, events_too_old: 0, events_over_cap: 0,
        record_id: null, outcome: "skipped", skip_reason: "blocked_by_hook", output_text: null, notification_id: null },
    } as unknown as ScheduleRunInfo;
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
      if (url === "/api/v1/schedule_admin/runs") return Response.json({ status: "ok", result: { runs: [run], next_before: null,
        routine_skips: { skipped_gate: 0, skipped_overlap: 0 } } });
      throw new Error(`Unexpected request: ${url}`);
    }));
    await act(async () => root.render(<RunHistory schedule={subscription(null)} open version={0} />));
    expect(container.textContent).toContain("Skipped (a prompt hook blocked it)");
  });

  it("offers the subscription type for a new schedule and sends its stream with the create", async () => {
    const writes: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      let result: unknown;
      if (url === "/api/v1/schedule_admin/project_list") result = { schedules: [], limits: {
        default_timezone: "UTC", name_max_chars: 80, requirement_max_chars: 10000, run_timeout_default_seconds: 60,
        run_timeout_max_seconds: 2592000, roles: ["planner"], gate_command_max_chars: 2000, gate_timeout_default_seconds: 30,
        gate_timeout_max_seconds: 300, max_parallel_runs_limit: 10, concurrency_max: 100,
        notify_targets: [{ channel: "lark", chat: "ou_synthetic", label: "Lark · ou_synthetic", channel_enabled: true }],
      } };
      else if (url === "/api/v1/managers/list") result = { managers: [{ id: "manager", enabled: true }], max_enabled: 8 };
      else if (url === "/api/v1/registry/view") result = { providers: [], profiles: [] };
      else if (url === "/api/v1/schedule_admin/create") {
        writes.push(JSON.parse(String(init?.body)).fields);
        result = { schedule: { ...subscription(null), ...JSON.parse(String(init?.body)).fields } };
      } else if (url === "/api/v1/schedule_admin/preview") result = { valid: true, next: [], description: "Every hour" };
      else if (url === "/api/v1/schedule_admin/runs") result = { runs: [], next_before: null, routine_skips: { skipped_gate: 0, skipped_overlap: 0 } };
      else throw new Error(`Unexpected request: ${url}`);
      return Response.json({ status: "ok", result });
    }));
    const project = { project_id: "project", name: "Example", work_dir: "/srv/example" } as Project;
    await act(async () => root.render(<ProjectSchedulesSection project={project} linked="" onLink={() => {}} />));
    const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
    await act(async () => button("New schedule").click());
    await act(async () => container.querySelector<HTMLInputElement>('input[type="radio"][value="subscription"]')!.click());
    const type = (suffix: string, value: string) => {
      const input = container.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[id$="-${suffix}"]`)!;
      const prototype = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    await act(async () => { type("name", "Gossip"); type("cron", "5 * * * *"); type("requirement", "Pick the funny ones"); type("source", "stream"); type("budget", "0.5"); });
    await act(async () => button("Create").click());
    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatchObject({
      kind: "subscription", name: "Gossip", cron: "5 * * * *", requirement: "Pick the funny ones", run_budget_usd: 0.5,
      subscription: { source_id: "stream", chat_kinds: ["group", "topic"], sender_kinds: ["user", "app"], exclude_mentions_me: true,
        include_history: false, max_age_minutes: 180, max_events: 500, text_max_chars: 200, quiet_word: "SKIP" },
    });
    expect(writes[0]).not.toHaveProperty("work_dir");
    expect(writes[0]).not.toHaveProperty("notify_target");
    expect((writes[0] as { subscription: object }).subscription).not.toHaveProperty("output_kind");
  });

  it("fills a new subscription from the leads and intents presets, and drops a group target for intents", async () => {
    const writes: Record<string, unknown>[] = [];
    const preset = (kind: "leads" | "intents") => ({
      kind: "subscription", name: kind === "intents" ? "意图识别" : "消息线索",
      cron: kind === "intents" ? "*/10 * * * *" : "7 * * * *", timezone: "UTC",
      requirement: kind === "intents" ? "Find intents" : "Find leads",
      run_budget_usd: kind === "intents" ? 0.3 : 0.6, daily_budget_usd: kind === "intents" ? 3 : 10, notify: "failures",
      subscription: {
        source_id: "lark-user", chat_kinds: ["p2p", "group", "topic"], sender_kinds: kind === "intents" ? ["user", "app"] : ["user"],
        include_history: true, max_age_minutes: 10080, max_events: 500, text_max_chars: 500,
        quiet_word: "NONE", output_header: "", output_kind: kind,
      },
    });
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      let result: unknown;
      if (url === "/api/v1/schedule_admin/project_list") result = { schedules: [], limits: {
        default_timezone: "UTC", name_max_chars: 80, requirement_max_chars: 10000, run_timeout_default_seconds: 60,
        run_timeout_max_seconds: 2592000, roles: ["planner"], gate_command_max_chars: 2000, gate_timeout_default_seconds: 30,
        gate_timeout_max_seconds: 300, max_parallel_runs_limit: 10, concurrency_max: 100,
        notify_targets: [
          { channel: "lark", chat: "oc_group", label: "Lark · oc_group (bound to manager)", channel_enabled: true },
          { channel: "lark", chat: "ou_direct", label: "Lark · ou_direct", channel_enabled: true },
        ],
      } };
      else if (url === "/api/v1/managers/list") result = { managers: [{ id: "manager", enabled: true }], max_enabled: 8 };
      else if (url === "/api/v1/registry/view") result = { providers: [], profiles: [] };
      else if (url === "/api/v1/schedule_admin/subscription_preset") result = { fields: preset(JSON.parse(String(init?.body)).output_kind) };
      else if (url === "/api/v1/schedule_admin/create") {
        writes.push(JSON.parse(String(init?.body)).fields);
        result = { schedule: { ...subscription(null), ...JSON.parse(String(init?.body)).fields } };
      } else if (url === "/api/v1/schedule_admin/preview") result = { valid: true, next: [], description: "Every ten minutes" };
      else if (url === "/api/v1/schedule_admin/runs") result = { runs: [], next_before: null, routine_skips: { skipped_gate: 0, skipped_overlap: 0 } };
      else throw new Error(`Unexpected request: ${url}`);
      return Response.json({ status: "ok", result });
    }));
    const project = { project_id: "project", name: "Example", work_dir: "/srv/example" } as Project;
    await act(async () => root.render(<ProjectSchedulesSection project={project} linked="" onLink={() => {}} />));
    const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
    await act(async () => button("New schedule").click());
    await act(async () => container.querySelector<HTMLInputElement>('input[type="radio"][value="subscription"]')!.click());
    const target = container.querySelector<HTMLSelectElement>('[id$="-target"]')!;
    await act(async () => {
      const value = [...target.options].find((option) => option.textContent?.includes("oc_group"))!.value;
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(target, value);
      target.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => button("消息线索").click());
    expect(container.querySelector<HTMLInputElement>('[id$="-name"]')!.value).toBe("消息线索");
    expect(container.querySelector<HTMLInputElement>('[id$="-cron"]')!.value).toBe("7 * * * *");
    expect(target.value).toContain("oc_group");
    await act(async () => button("意图识别").click());
    expect(container.querySelector<HTMLInputElement>('[id$="-name"]')!.value).toBe("意图识别");
    expect(container.querySelector<HTMLInputElement>('[id$="-cron"]')!.value).toBe("*/10 * * * *");
    expect(target.value).toBe("");
    const labels = [...target.options].map((option) => option.textContent ?? "");
    expect(labels.some((label) => label.includes("oc_group"))).toBe(false);
    expect(labels.some((label) => label.includes("ou_direct"))).toBe(true);
    expect(container.textContent).toContain("意图识别只推到私聊。");
    await act(async () => button("Create").click());
    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatchObject({
      name: "意图识别", cron: "*/10 * * * *",
      subscription: { source_id: "lark-user", output_kind: "intents", quiet_word: "NONE" },
    });
    expect(writes[0]).not.toHaveProperty("notify_target");
  });
});
