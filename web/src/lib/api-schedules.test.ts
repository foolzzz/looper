// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { fetchAllSchedules, fetchSchedules, fetchSchedule, fetchScheduleRuns, fetchScheduleNotifications,
  previewSchedule } from "@/lib/api";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

it("reads full definitions, limits, history, notifications and invalid previews through v1", async () => {
  const schedule = { schedule_id: "sched-example", requirement: "Read reports. ".repeat(100) };
  const preview = { valid: false, cron: "invalid", timezone: "UTC", next: [], error: "Invalid cron" };
  const results = [ { schedules: [schedule] }, { schedules: [schedule], limits: { name_max_chars: 80 } },
    { schedule }, { runs: [{ run_id: "srun-example" }], next_before: null, routine_skips: { skipped_gate: 8, skipped_overlap: 2 } },
    { notifications: [{ notification_id: "note-example" }] }, preview ];
  const fetcher = vi.fn();
  for (const result of results) fetcher.mockResolvedValueOnce(Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  expect(await fetchAllSchedules()).toEqual([schedule]);
  expect(await fetchSchedules("proj-example")).toEqual(results[1]);
  expect(await fetchSchedule("sched-example")).toEqual(schedule);
  expect(await fetchScheduleRuns("sched-example", { limit: 20, before: null, skips: "hide" })).toEqual(results[3]);
  expect(await fetchScheduleNotifications("sched-example", 2)).toEqual([{ notification_id: "note-example" }]);
  expect(await previewSchedule({ cron: "invalid", count: 2, gated: false })).toEqual(preview);
  expect(fetcher.mock.calls.map(([url, options]) => [url, options.method, JSON.parse(options.body)])).toEqual([
    ["/api/v1/schedule_admin/list", "POST", {}],
    ["/api/v1/schedule_admin/project_list", "POST", { project_id: "proj-example" }],
    ["/api/v1/schedule_admin/get", "POST", { schedule_id: "sched-example" }],
    ["/api/v1/schedule_admin/runs", "POST", { schedule_id: "sched-example", limit: 20, before: null, skips: "hide" }],
    ["/api/v1/schedule_admin/notifications", "POST", { schedule_id: "sched-example", limit: 2 }],
    ["/api/v1/schedule_admin/preview", "POST", { cron: "invalid", count: 2, gated: false }],
  ]);
});

it("preserves typed errors and refuses reads after the session ends", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "error", error: { code: "not_found", message: "No schedule" } }, { status: 404 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(fetchSchedule("sched-missing")).rejects.toMatchObject({ code: "not_found", message: "No schedule" });
  fetcher.mockResolvedValueOnce(new Response(null, { status: 401 }));
  installSessionGuard();
  await expect(fetchSchedule("sched-example")).rejects.toBeInstanceOf(SessionEndedError);
  await expect(fetchAllSchedules()).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledTimes(2);
});
