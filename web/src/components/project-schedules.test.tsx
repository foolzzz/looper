// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlannerSessionStatus, ProjectSchedulesSection, ResetPlannerSession, RunStatusBadge, RunHistory, LastRun } from "@/components/project-schedules";
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
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
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
    expect(JSON.parse(String(logCalls()[0][1]?.body))).toEqual({ run_id: "srun-failed", tail_lines: 200 });
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

  it("opens the schedule and target run from its nested project link", async () => {
    stub((path) => {
      if (path === "schedule_admin/project_list") return { schedules: [storedSchedule("command")], limits: {
        default_timezone: "UTC", roles: [], notify_targets: [], name_max_chars: 80, requirement_max_chars: 10000,
      } };
      if (path === "managers/list") return { managers: [] };
      if (path === "registry/view") return { profiles: [] };
    });
    await act(async () => root.render(<ProjectSchedulesSection project={{ project_id: "project" } as Project}
      linked="sched%2Fa/runs/srun-failed" onLink={() => {}} />));
    expect(container.querySelector('[aria-controls="schedule-sched/a-panel"]')?.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector('[data-run-id="srun-failed"] button')?.getAttribute("aria-expanded")).toBe("true");
  });

  it("reports a retained-history miss and an empty statistics window", async () => {
    stub(() => ({ ...respond([]), stats: { day: { ...stats, runs: 0 }, week: { ...stats, runs: 0 } } }));
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-gone" />));
    expect(container.textContent).toContain("This run is no longer kept");
    expect(container.textContent).toContain("No runs in the last 24 h");
  });

  it("stops looking for a linked run after 200 records", async () => {
    let count = 0;
    stub(() => {
      const rows = Array.from({ length: 20 }, () => run({ run_id: `srun-${++count}` }));
      return respond(rows, rows.at(-1)!.run_id);
    });
    await act(async () => root.render(<RunHistory schedule={storedSchedule("command")} open version={0} runId="srun-gone" />));
    expect(count).toBe(200);
    expect(requests).toHaveLength(10);
    expect(container.textContent).toContain("This run is no longer kept");
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
