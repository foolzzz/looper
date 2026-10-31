// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { fetchAllSchedules, fetchSchedules, fetchSchedule, fetchScheduleRuns, fetchScheduleNotifications,
  previewSchedule, fetchRunLog, fetchRunLogTail, downloadRunLog } from "@/lib/api";
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

it("passes the selected run status without changing returned statistics", async () => {
  const stats = { runs: 3, succeeded: 2, failed: 1, success_rate: 2 / 3, avg_seconds: 7, max_seconds: 20 };
  const result = { runs: [], next_before: null, routine_skips: { skipped_gate: 0, skipped_overlap: 1 },
    stats: { day: stats, week: stats } };
  const fetcher = vi.fn().mockResolvedValue(Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  expect(await fetchScheduleRuns("sched-example", { status: "failed" })).toEqual(result);
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ schedule_id: "sched-example", status: "failed" });
});

it("reads a log tail through v1 and preserves the missing-log reason", async () => {
  const result = { size: 40, offset: 12, next_offset: null, data_base64: "bG9nCg==", complete: false, truncated: true };
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json({ status: "ok", result }))
    .mockResolvedValueOnce(Response.json({ status: "error", error: { code: "not_found", message: "The log was removed by retention" } }, { status: 404 }));
  vi.stubGlobal("fetch", fetcher);
  expect(await fetchRunLog("srun-example", { tail_lines: 200 })).toEqual(result);
  expect(fetcher.mock.calls[0][0]).toBe("/api/v1/schedule_admin/run_log");
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ run_id: "srun-example", tail_lines: 200 });
  await expect(fetchRunLog("srun-example")).rejects.toMatchObject({ message: "The log was removed by retention" });
});

it("downloads every log page as original bytes and reports byte progress", async () => {
  // Split a UTF-8 character across pages and retain bytes that are not valid UTF-8.
  const bytes = new Uint8Array([0x00, 0xff, 0xe4, 0xb8, 0xad, 0x0a, 0x80]);
  const pages = [bytes.slice(0, 3), bytes.slice(3, 5), bytes.slice(5)];
  let offset = 0;
  const fetcher = vi.fn();
  for (const [index, page] of pages.entries()) {
    const nextOffset = offset + page.length;
    fetcher.mockResolvedValueOnce(Response.json({ status: "ok", result: {
      size: bytes.length, offset, next_offset: index === pages.length - 1 ? null : nextOffset,
      data_base64: btoa(String.fromCharCode(...page)), complete: false, truncated: false,
    } }));
    offset = nextOffset;
  }
  vi.stubGlobal("fetch", fetcher);
  const progress = vi.fn();
  const blob = await downloadRunLog("srun-example", progress);
  expect(blob.type).toBe("text/plain");
  expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes);
  expect(fetcher.mock.calls.map(([, options]) => JSON.parse(options.body))).toEqual([
    { run_id: "srun-example", offset: 0, limit_bytes: 4 * 1024 * 1024 },
    { run_id: "srun-example", offset: 3, limit_bytes: 4 * 1024 * 1024 },
    { run_id: "srun-example", offset: 5, limit_bytes: 4 * 1024 * 1024 },
  ]);
  expect(progress.mock.calls).toEqual([[3, 7], [5, 7], [7, 7]]);
});

it("downloads an empty log once and stops at its end", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result: {
    size: 0, offset: 0, next_offset: null, data_base64: "", complete: true, truncated: false,
  } }));
  vi.stubGlobal("fetch", fetcher);
  expect((await downloadRunLog("srun-empty")).size).toBe(0);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it.each(["a line with no newline", "partial\nlast line"]) ("bounds the tail walk and discards an incomplete leading line: %s", async (tail) => {
  const window = 4 * 1024 * 1024;
  const fetcher = vi.fn(async (_url, init) => {
    const body = JSON.parse(init.body);
    return Response.json({ status: "ok", result: { size: 5 * window, offset: body.offset ?? 4 * window,
      next_offset: null, data_base64: btoa(body.offset == null ? tail : "prefix"), complete: true, truncated: false } });
  });
  vi.stubGlobal("fetch", fetcher);
  expect((await fetchRunLogTail("run")).text).toBe(tail.includes("\n") ? "last line" : "");
  expect(fetcher).toHaveBeenCalledTimes(4);
});
